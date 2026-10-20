"""Helpers shared by the build scripts (main, compat)"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import zipfile

try:
    import tomllib
except ImportError:
    # for Python 3.9 and 3.10, which lack tomllib
    import tomli as tomllib  # type: ignore
from pathlib import Path

from ..common import fail, normalize_version, to_native, to_windows
from ..log import HEADING, SUBHEADING
from ..patcher.fixes import APPLIED
from ..patcher.swf_patcher import format_fix
from ..paths import FROZEN, MANIFEST_DIR, MANIFEST_DIRS, REPO_ROOT
from ..witcher3.bundle import BundleError, make_entry, write_bundle
from ..witcher3.metadata_store import MetadataStoreError, make_metadata_store

log = logging.getLogger(__name__)

DEFAULT_PROFILE = REPO_ROOT / "config" / "profile_default.txt"
REVIEWED_WARNINGS = REPO_ROOT / "config" / "reviewed_warnings.txt"
# Manifests of the compat patches built with build-compat.py (see compat.py's write_patch_manifest)
COMPAT_MANIFEST_DIR = MANIFEST_DIR / "compat"
# Files the main mod ships as they are (scripts, the settings menu), laid out as in its zip:
# mods/<the mod's folder>/, and bin/ (for the game's bin/ folder)
MOD_FILES = REPO_ROOT / "mod_files"

REQUIRED_CONFIG_KEYS = ("working_dir",)

# While metadata_store.py is new, the command-line tools also have wcc_lite make metadata.store, and fail
# unless both are identical. Never in the frozen tool: users don't have wcc_lite.
COMPARE_STORE_WITH_WCC_LITE = not FROZEN

# A build's version: the base version (created_mod_version in config.toml), then the frame rate
BUILD_VERSION = re.compile(r"(v.+?)-(\d+)fps")
# A base version: numbers, then an optional suffix, e.g. v0.3, v0.3.1, v0.3.2dev, v0.3beta
VERSION = re.compile(r"v(\d+(?:\.\d+)*)([a-z][a-z0-9]*)?")
# Versions with the same numbers, from least to most recent; other suffixes come before these
SUFFIXES = ("beta", "dev", "")
# The frame rate of manifests from before the 60 and 120 fps builds
OLD_MANIFEST_FPS = 120
# The frame rates of a release's builds, in the order they are built and reported
RELEASE_FPS = (120, 60)

# wcc_lite lines that appear on every run and are harmless
NOISE = [re.compile(p) for p in (
    r"\[Error\]\[Core\] CreateFile failed to open '.*\.(?:ini|settings)' for reading.*0x[23]",
    r"\[Error\]\[Assert\] .*depotDirectory\.cpp:\d+.*Depot directory path should end with",
    r"\[Error\]\[Assert\] .*soundFileLoader\.cpp:\d+.*Always loaded bank is not in the banks array",
)]

def run_command(cmd, cwd=None, filter_predicate=None, detach=False):
    """Run a command, streaming output; optionally filter lines before printing"""
    log.info("$ " + " ".join(str(c) for c in cmd))
    try:
        if detach:
            subprocess.Popen(
                [str(c) for c in cmd], cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return
        with subprocess.Popen(
            [str(c) for c in cmd], cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        ) as proc:
            for line in proc.stdout:
                line = line.rstrip()
                if filter_predicate is None or not filter_predicate(line):
                    log.info(line)
    except FileNotFoundError:
        fail(f"executable or working directory not found: {cmd[0]}")
    if proc.returncode != 0:
        fail(f"command failed with exit code {proc.returncode}: {cmd[0]}")

def wcc_lite(*args):
    """Run wcc_lite, optionally filtering known noise from the output"""
    config = load_config()
    paths = config_paths(config)

    def filter_noise(line):
        return config['silence_known_noise'] and any(p.search(line) for p in NOISE)

    wcc_lite_path = paths['wcc_lite']
    run_command([wcc_lite_path] + list(args), cwd=wcc_lite_path.parent, filter_predicate=filter_noise)

def pack_content(patched_dir, content_dir):
    """Pack the patched files into blob0.bundle and create metadata.store"""
    log.info("Packing blob0.bundle", extra=HEADING)
    files = sorted(p for p in patched_dir.rglob("*") if p.is_file())
    bundle = content_dir / "blob0.bundle"
    try:
        bundle.write_bytes(write_bundle(make_entry(p.relative_to(patched_dir), p.read_bytes()) for p in files))
    except BundleError as e:
        fail(f"can't pack {patched_dir}: {e}")
    log.info(f"Packed {len(files)} files into {bundle} ({bundle.stat().st_size / 2**20:.1f} MiB)")

    store_path = content_dir / "metadata.store"
    if COMPARE_STORE_WITH_WCC_LITE:
        log.info("Running wcc_lite metadatastore (to compare)", extra=HEADING)
        wcc_lite("metadatastore", "-noui", f"-path={to_windows(content_dir)}")
        if not store_path.is_file():
            fail("wcc_lite did not produce metadata.store.")

    log.info("Writing metadata.store", extra=HEADING)
    try:
        store = make_metadata_store([(bundle.name, bundle.read_bytes())])
    except MetadataStoreError as e:
        fail(f"can't make metadata.store for {bundle}: {e}")
    if COMPARE_STORE_WITH_WCC_LITE:
        if store_path.read_bytes() != store:
            store_path.with_name("metadata.store.ours").write_bytes(store)
            fail(f"metadata.store differs from wcc_lite's; wcc_lite's is in {store_path}, ours in metadata.store.ours")
        log.info("Identical to wcc_lite's")
    store_path.write_bytes(store)
    log.info(f"Wrote {store_path} ({len(store)} bytes)")

def load_config(path=None, section=None):
    """Read config.toml (or the given file) and perform basic validation"""
    path = path or REPO_ROOT / "config" / "config.toml"
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        fail(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        fail(f"invalid config file {path}: {e}")
    if section is not None:
        if section not in config:
            fail(f"missing section {section} in {path}")
        return config[section]

    validate_config(config, path)
    return config

def validate_config(config, path):
    """Validate a config file; fail if not valid"""
    paths = config.get("paths", {})
    required = REQUIRED_CONFIG_KEYS + (("wcc_lite",) if COMPARE_STORE_WITH_WCC_LITE else ())
    missing = [k for k in required if k not in paths]
    if missing:
        fail(f"missing in [paths] of {path}: {', '.join(missing)}")

def config_paths(config):
    """The config's paths, converted to Path objects usable by this Python"""
    return {k: to_native(v) for k, v in config["paths"].items()}

def print_file_result(rel, status, report=None, error=None, *, is_reviewed=None):
    """on_file callback for patcher_core.patch_files: the result, and anything to check by hand.
    Bind is_reviewed(rel, warning) (see reviewed_warnings.py) to hide warnings already checked."""
    if status == "failed":
        log.error(f"{rel}: {error}")
        return
    if status == "kept":
        log.info(f"KEEP {rel}: {report['old_fps']:g} fps, already at or above the target "
                 f"{report['target_fps']:g} fps")
        _print_fixes(report)
        return

    if report["new_fps"] is None:
        log.info(f"{rel}: {report['old_fps']:g} fps, already at or above the target {report['target_fps']:g} fps; "
                 "fixed only")
    else:
        log.info(f"{rel}: {report['old_fps']:g} -> {report['new_fps']:g} fps")
    _print_fixes(report)
    if report["unmet_rules"]:
        log.error(f"  {len(report['unmet_rules'])} explicit rule(s) not followed (listed at the end)")
    warnings = [warning for warning in report["warnings"]
                if not (is_reviewed and is_reviewed(rel, warning))]
    if warnings:
        log.warning(f"  {len(warnings)} unreviewed warning(s):"
                    + "".join(f"\n    [{warning['key']}] {warning['message']}" for warning in warnings))

def _print_fixes(report):
    """The fixes' results; those that changed nothing are listed again at the end, as rule issues"""
    for fix in report.get("fixes", []):
        log.log(logging.INFO if fix["result"] == APPLIED else logging.ERROR, f"  {format_fix(fix)}")

def print_rule_issues(issues):
    """Print patcher_core.RuleIssues. The build still finishes, so these must be impossible to miss."""
    if issues.warnings:
        log.warning(f"{len(issues.warnings)} INHERITED SETTING(S) NO LONGER APPLY: the other mod changed what "
                    "they matched", extra=SUBHEADING)
        for warning in issues.warnings:
            log.warning(f"  {warning}")
        log.info("")
    if issues.errors:
        log.error(f"{len(issues.errors)} EXPLICIT RULE(S) NOT FOLLOWED: the result is NOT patched entirely as "
                  "configured", extra=SUBHEADING)
        for error in issues.errors:
            log.error(f"  {error}")
        log.info("")

def enforce_strict(kept, rule_errors, unreviewed_warnings=()):
    """--strict: fail unless every file was patched, exactly as configured, with no unreviewed warnings.
    (A file that failed fails the build even without --strict.)"""
    problems = []
    if kept:
        problems.append(f"{len(kept)} file(s) left unchanged, as they are already at or above their target frame rate")
    if rule_errors:
        problems.append(f"{len(rule_errors)} explicit rule(s) not followed")
    if unreviewed_warnings:
        problems.append(f"{len(unreviewed_warnings)} unreviewed warning(s)")
    if problems:
        fail("--strict: " + "; ".join(problems) + " (see above; use --no-strict to build anyway)")

def copy_files(src_dir, dst_dir):
    """Copy the files under src_dir into dst_dir (merging folders), without their metadata: copying
    a folder's permissions fails on Windows drives in WSL. Returns the copied files' paths, relative."""
    copied = []
    for f in sorted(Path(src_dir).rglob("*")):
        if f.is_file():
            target = Path(dst_dir) / f.relative_to(src_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
            copied.append(f.relative_to(src_dir))
    return copied

def add_mod_files(output_dir, mod_name):
    """Copy MOD_FILES into the mod's output tree (mods/mod_name/, bin/)"""
    log.info("Adding scripts and settings menu", extra=HEADING)
    unexpected = [p.relative_to(MOD_FILES).as_posix() for p in [*MOD_FILES.iterdir(), *(MOD_FILES / "mods").iterdir()]
                  if p.relative_to(MOD_FILES).as_posix() not in ("mods", "bin", f"mods/{mod_name}")]
    if unexpected:
        fail(f"{MOD_FILES} should only hold mods/{mod_name}/ and bin/, not {', '.join(unexpected)}")
    for path in copy_files(MOD_FILES, output_dir):
        log.info(f"Added {path.as_posix()}")

def zip_tree(src_dir, zip_out):
    """Zip every file under src_dir, keeping the tree's layout at the root of the archive"""
    log.info("Creating output .zip", extra=HEADING)
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    zip_out.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src_dir))
    log.info(f"Created {zip_out} ({zip_out.stat().st_size:,} bytes)")

def sha256_file(path):
    """SHA-256 of a file's contents"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def patcher_commit():
    """(commit, uncommitted) for the code doing the build: the git commit hash of this checkout, and
    whether tracked files have uncommitted changes. (None, None) if git can't tell (e.g. no checkout)."""
    if FROZEN:
        return None, None   # no checkout, and the tool's folder may be inside someone else's
    def git(*args):
        # --no-optional-locks: don't let `git status` refresh the index, so nothing is written
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    try:
        return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.CalledProcessError):
        return None, None

def build_version(base_version, fps):
    """The version of a build at fps, e.g. v0.3-60fps"""
    return f"{normalize_version(base_version)}-{fps}fps"

def split_build_version(version):
    """(base version, fps) for a build's version (see build_version); fps is None for versions
    without a frame rate (manifests from before the 60 and 120 fps builds)"""
    version = normalize_version(version)
    match = BUILD_VERSION.fullmatch(version)
    return (match[1], int(match[2])) if match else (version, None)

def version_key(version):
    """Sort key for versions like v0.3.1, v0.3beta or v0.3-60fps (the frame rate is ignored; see
    build_fps), and other mods' versions like v1.55; None if it isn't one. The numbers compare as
    integers (v0.3.10 comes after v0.3.9), then the suffix (see SUFFIXES)."""
    match = VERSION.fullmatch(split_build_version(version)[0])
    if not match:
        return None
    suffix = match[2] or ""
    return tuple(int(number) for number in match[1].split(".")), SUFFIXES.index(suffix) if suffix in SUFFIXES else -1

def is_dev_version(version):
    key = version_key(version)
    return key is not None and key[1] == SUFFIXES.index("dev")

def build_fps(version):
    """The frame rate of the build a version names"""
    return split_build_version(version)[1] or OLD_MANIFEST_FPS

def release_manifest_paths():
    """{version: path} of every release manifest (<version>.json) in MANIFEST_DIRS. A version found in
    several of them (a downloaded copy of one the tool also ships) uses the copy built last."""
    def created_at(path):
        return (load_manifest(path) or {}).get("created_at") or ""
    found = {}
    for directory in MANIFEST_DIRS:
        for path in directory.glob("*.json"):
            if path.stem not in found or created_at(path) > created_at(found[path.stem]):
                found[path.stem] = path
    return found

def release_manifests(fps):
    """(version, path) of each release manifest of a build at fps, oldest first"""
    found = [(version, path) for version, path in release_manifest_paths().items()
             if version_key(version) is not None and build_fps(version) == fps]
    return sorted(found, key=lambda item: version_key(item[0]))

def latest_manifests():
    """{fps: path} of the most recent release manifest at each release frame rate, dev builds included"""
    return {fps: manifests[-1][1] for fps in RELEASE_FPS if (manifests := release_manifests(fps))}

def manifest_path(version):
    """Where build-main.py writes a mod release's manifest: manifests/<version>.json"""
    return MANIFEST_DIR / f"{normalize_version(version)}.json"

def load_manifest(version_or_path):
    """A manifest, given a release's version (see release_manifest_paths) or a path to it; None if it
    doesn't exist"""
    if str(version_or_path).endswith(".json"):
        path = Path(version_or_path)
    else:
        path = release_manifest_paths().get(normalize_version(version_or_path))
    if path is None or not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def write_manifest(version, records, extra=None):
    """Write manifests/<version>.json for this mod release.

    records maps each patched file's relative path to its record: SHA-256 and size of the source and
    output files, SHA-256 of the SWF in each (see patcher/redswf.py's swf_sha256), "fps" (the frame
    rate it was patched to) and "report" (its full rescale report, see patcher/swf_patcher/report.py),
    which includes the speed rules and include/exclude lists it was patched with. extra holds the
    top-level fields, including "fps", the build's frame rate."""
    manifest = {"created_mod_version": normalize_version(version)}
    if extra:
        manifest.update(extra)
    manifest["files"] = records

    path = manifest_path(version)
    write_json(path, manifest)
    log.info(f"Wrote manifest {path} ({len(records)} files)")
    return path

def compat_manifest_path(version, mod_name, mod_version):
    """Where the manifest of a compat patch for one build of a release (version, e.g. v0.3-60fps) lives;
    named like the patch's .zip"""
    return COMPAT_MANIFEST_DIR / f"{normalize_version(version)}_{mod_name}_{normalize_version(mod_version)}.json"

def write_json(path, data):
    """Write a manifest (replacing any file there), in the format all manifests use"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")

def install_mod(mod_dir, game_path, bin_dir=None):
    """Copy <mod_dir> (a directory whose name is the mod's name) into <game_path>/mods, and the files
    in bin_dir (the settings menu), if given, into <game_path>/bin"""
    log.info("Installing mod to game folder", extra=HEADING)
    if not game_path.is_dir():
        fail(f"game path not found: {game_path}")
    dest = game_path / "mods" / Path(mod_dir).name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(mod_dir, dest)
    log.info(f"Installed mod into {dest}")
    if bin_dir is not None and Path(bin_dir).is_dir():
        for path in copy_files(bin_dir, game_path / "bin"):
            log.info(f"Installed {game_path / 'bin' / path}")

def launch_game(game_path):
    """Attempt to launch the game executable"""
    log.info("Launching game", extra=HEADING)
    exe_path = game_path / "bin/x64_dx12/witcher3.exe"
    run_command([exe_path], cwd=exe_path.parent, detach=True)

def install_and_launch(mod_dir, game_path: Path, install, launch, bin_dir=None):
    """Copy the built mod (and the files in bin_dir, if given) into the game folder, then launch the
    game if asked. A missing game_path (None) means nothing can be installed or launched"""
    if not game_path and (install or launch):
        log.warning("game_path not set in config.toml; will ignore --install/--launch")
        return
    if not game_path.exists():
        log.warning("game_path set in config.toml does not exist! Will ignore --install/--launch")
        return
    if install:
        install_mod(mod_dir, game_path, bin_dir)
        if launch:
            launch_game(game_path)
    elif launch:
        log.warning("\n--launch used without --install: not launching game with old mod version")

def finish_work_dir(work_dir, keep_work):
    """Keep or drop the work tree once the build is done"""
    if keep_work:
        log.info(f"Keeping work files in {work_dir}")
    else:
        log.info(f"Removing {work_dir}", extra=HEADING)
        shutil.rmtree(work_dir, ignore_errors=True)