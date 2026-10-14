"""Helpers shared by the build scripts (main, compat)"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile

try:
    import tomllib
except ImportError:
    # for Python 3.9 and 3.10, which lack tomllib
    import tomli as tomllib  # type: ignore
from pathlib import Path

from colorama import Back, Fore, Style

from ..common import fail, normalize_version, print_step_header, to_native, to_windows

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPO_ROOT / "config" / "profile_default.txt"
REVIEWED_WARNINGS = REPO_ROOT / "config" / "reviewed_warnings.txt"
MANIFEST_DIR = REPO_ROOT / "manifests"

REQUIRED_CONFIG_KEYS = ("working_dir", "wcc_lite")

# wcc_lite lines that appear on every run and are harmless
NOISE = [re.compile(p) for p in (
    r"\[Error\]\[Core\] CreateFile failed to open '.*\.(?:ini|settings)' for reading.*0x[23]",
    r"\[Error\]\[Assert\] .*depotDirectory\.cpp:\d+.*Depot directory path should end with",
    r"\[Error\]\[Assert\] .*soundFileLoader\.cpp:\d+.*Always loaded bank is not in the banks array",
)]

def run_command(cmd, cwd=None, filter_predicate=None, detach=False):
    """Run a command, streaming output; optionally filter lines before printing"""
    print("$", " ".join(str(c) for c in cmd), flush=True)
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
                    print(line, flush=True)
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
    print_step_header("Running wcc_lite pack")
    wcc_lite("pack", f"-dir={to_windows(patched_dir)}", f"-outdir={to_windows(content_dir)}")

    print_step_header("Running wcc_lite metadatastore")
    wcc_lite("metadatastore", "-noui", f"-path={to_windows(content_dir)}")

    if not (content_dir / "metadata.store").is_file() or not any(content_dir.glob("blob*.bundle")):
        fail("wcc_lite did not produce blob*.bundle and metadata.store.")

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
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in paths]
    if missing:
        fail(f"missing in [paths] of {path}: {', '.join(missing)}")

def config_paths(config):
    """The config's paths, converted to Path objects usable by this Python"""
    return {k: to_native(v) for k, v in config["paths"].items()}

def print_file_result(rel, status, report=None, error=None, *, is_reviewed=None):
    """on_file callback for patcher_core.patch_files: the result, and anything to check by hand.
    Bind is_reviewed(rel, warning) (see reviewed_warnings.py) to hide warnings already checked."""
    if status == "failed":
        print(f"{Style.BRIGHT}{Fore.RED}SKIP {rel}: {error}{Style.RESET_ALL}", file=sys.stderr, flush=True)
        return
    if status == "kept":
        print(f"KEEP {rel}: {report['old_fps']:g} fps, already at or above the target "
              f"{report['target_fps']:g} fps", flush=True)
        return

    print(f"{rel}: {report['old_fps']:g} -> {report['new_fps']:g} fps", flush=True)
    if report["unmet_rules"]:
        print(f"{Style.BRIGHT}{Fore.RED}  {len(report['unmet_rules'])} explicit rule(s) not followed "
              f"(listed at the end){Style.RESET_ALL}", flush=True)
    warnings = [warning for warning in report["warnings"]
                if not (is_reviewed and is_reviewed(rel, warning))]
    if warnings:
        print(f"{Fore.YELLOW}  {len(warnings)} unreviewed warning(s):{Style.RESET_ALL}", flush=True)
        for warning in warnings:
            print(f"{Fore.YELLOW}    [{warning['key']}] {warning['message']}{Style.RESET_ALL}", flush=True)

def print_rule_issues(issues):
    """Print patcher_core.RuleIssues. The build still finishes, so these must be impossible to miss."""
    if issues.warnings:
        print(f"\n{Back.YELLOW}{Fore.BLACK} {len(issues.warnings)} INHERITED SETTING(S) NO LONGER APPLY: "
              f"the other mod changed what they matched {Style.RESET_ALL}", flush=True)
        for warning in issues.warnings:
            print(f"{Fore.YELLOW}  {warning}{Style.RESET_ALL}", flush=True)
        print(flush=True)
    if issues.errors:
        print(f"\n{Back.RED}{Fore.WHITE} {len(issues.errors)} EXPLICIT RULE(S) NOT FOLLOWED: "
              f"the result is NOT patched entirely as configured {Style.RESET_ALL}", flush=True)
        for error in issues.errors:
            print(f"{Style.BRIGHT}{Fore.RED}  {error}{Style.RESET_ALL}", flush=True)
        print(flush=True)

def enforce_strict(failed, kept, rule_errors, unreviewed_warnings=()):
    """--strict: fail unless every file was patched, exactly as configured, with no unreviewed warnings"""
    problems = []
    if failed:
        problems.append(f"{len(failed)} file(s) could not be patched")
    if kept:
        problems.append(f"{len(kept)} file(s) left unchanged, as they are already at or above their target frame rate")
    if rule_errors:
        problems.append(f"{len(rule_errors)} explicit rule(s) not followed")
    if unreviewed_warnings:
        problems.append(f"{len(unreviewed_warnings)} unreviewed warning(s)")
    if problems:
        fail("--strict: " + "; ".join(problems) + " (see above; use --no-strict to build anyway)")

def resolve_zip_path(zip_arg, default_name):
    """Output .zip for a build: bare name goes in the repo folder, .zip suffix added"""
    zip_arg = zip_arg or default_name
    if "/" not in zip_arg and "\\" not in zip_arg:
        zip_out = REPO_ROOT / zip_arg
    else:
        zip_out = Path(zip_arg)
    if zip_out.suffix.lower() != ".zip":
        zip_out = zip_out.with_name(zip_out.name + ".zip")
    return zip_out

def zip_tree(src_dir, zip_out):
    """Zip every file under src_dir, keeping the tree's layout at the root of the archive"""
    print_step_header("Creating output .zip")
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    zip_out.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src_dir))
    print(f"Created {zip_out} ({zip_out.stat().st_size:,} bytes)")

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
    def git(*args):
        # --no-optional-locks: don't let `git status` refresh the index, so nothing is written
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    try:
        return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.CalledProcessError):
        return None, None

def manifest_path(version):
    """Where a mod release's manifest lives: manifests/<version>.json"""
    return MANIFEST_DIR / f"{normalize_version(version)}.json"

def write_manifest(version, records, extra=None):
    """Write manifests/<version>.json for this mod release.

    records maps each patched file's relative path to its record: SHA-256 and size of the source and
    output files, SHA-256 of the SWF in each (see patcher/redswf.py's swf_sha256), "fps" (the frame
    rate it was patched to) and "report" (its full rescale report, see patcher/swf_patcher/report.py),
    which includes the fps rules and include/exclude lists it was patched with."""
    manifest = {"created_mod_version": normalize_version(version)}
    if extra:
        manifest.update(extra)
    manifest["files"] = records

    path = manifest_path(version)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"Wrote manifest {path} ({len(records)} files)")
    return path

def install_mod(mod_dir, game_path):
    """Copy <mod_dir> (a directory whose name is the mod's name) into <game_path>/mods"""
    print_step_header("Installing mod to game folder")
    if not game_path.is_dir():
        fail(f"game path not found: {game_path}")
    dest = game_path / "mods" / Path(mod_dir).name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(mod_dir, dest)
    print(f"Installed mod into {dest}")

def launch_game(game_path):
    """Attempt to launch the game executable"""
    print_step_header("Launching game")
    exe_path = game_path / "bin/x64_dx12/witcher3.exe"
    run_command([exe_path], cwd=exe_path.parent, detach=True)

def install_and_launch(mod_dir, game_path: Path, install, launch):
    """Copy the built mod into the game folder, then launch the game if asked.
    A missing game_path (None) means nothing can be installed or launched"""
    if not game_path and (install or launch):
        print("Warning: game_path not set in config.toml; will ignore --install/--launch")
        return
    if not game_path.exists():
        print("Warning: game_path set in config.toml does not exist! Will ignore --install/--launch")
        return
    if install:
        install_mod(mod_dir, game_path)
        if launch:
            launch_game(game_path)
    elif launch:
        print("\nWarning:--launch used without --install: not launching game with old mod version")

def finish_work_dir(work_dir, keep_work):
    """Keep or drop the work tree once the build is done"""
    if keep_work:
        print(f"Keeping work files in {work_dir}")
    else:
        print_step_header(f"Removing {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)