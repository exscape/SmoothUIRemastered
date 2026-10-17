#!/usr/bin/env python3

# build-compat.py -- take an existing mod .zip/.rar/.7z, apply framerate patches to the .redswf files
# that conflict with Smooth UI Remastered, and build compatibility patches containing only those
# patched files: one for each build of the Smooth UI release (120 and 60 fps), unless --only-fps is specified for development/testing.
#
# The files to patch and their settings come from the release's manifests (see manifests/), selected by
# --smooth-version, optionally with additions from an override file (--overrides; see
# config/compat_overrides_example.txt).
# Unless --only-fps or a dev hook is given, each patch also gets a manifest in manifests/compat/.
# This is used by build-main.py to tell which compat patches become outdated by a new release.
# Example usage: python build-compat.py modOtherUI.zip --mod-name OtherMod --mod-version 1.0 --smooth-version 0.3

import argparse
import datetime
import shutil
import zipfile
from functools import partial
from pathlib import Path

import py7zr
import rarfile
from colorama import Fore, Style

from ..common import fail, normalize_version, print_step_header, profile_key
from ..patcher import dev_mode, patcher_core
from ..patcher.fixes import check_fix_name
from ..patcher.patcher_core import PatchSettings
from ..patcher.swf_patcher import (
    SpeedRule,
    SpriteRef,
    parse_speed,
    parse_sprite_pattern,
)
from ..witcher3.bundle import BundleError, extract, read_bundle
from ..witcher3.redswf import swf_sha256
from .manifest_diff import SUFFIXES, version_key
from .profile import apply_overrides
from .reviewed_warnings import WarningReview, print_warning_summary, review_of
from .shared import (
    MANIFEST_DIR,
    REPO_ROOT,
    build_version,
    compat_manifest_path,
    config_paths,
    enforce_strict,
    finish_work_dir,
    install_and_launch,
    load_config,
    load_manifest,
    pack_content,
    patcher_commit,
    print_file_result,
    print_rule_issues,
    split_build_version,
    write_json,
    zip_tree,
)


# rarfile looks for an external unrar in PATH and in the current directory. When neither has it,
# point it at the UnRAR.exe bundled in this repo, so .rar mods work from any working directory.
def setup_rar_tool():
    if shutil.which(rarfile.UNRAR_TOOL):
        return # a system unrar is preferred (e.g. under WSL)
    bundled_unrar = REPO_ROOT / "UnRAR.exe"
    if bundled_unrar.is_file():
        rarfile.UNRAR_TOOL = str(bundled_unrar)

setup_rar_tool()

# Used to name the generated mod. The "mod000_" prefix sorts before the main mod and
# (nearly) any other mod, so the compatibility patch loads first and wins conflicts.
MAIN_MOD_NAME = "SmoothUIRemastered"

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Build a Smooth UI compatibility patch for an existing mod .zip/.rar/.7z")

    ap.add_argument("mod_archive", type=Path,
                    help="the other mod's .zip/.rar/.7z (must contain blob*.bundle)")
    ap.add_argument("--mod-name", required=True,
                    help="name of the other mod; only used to name the generated patch file")
    ap.add_argument("--mod-version", required=True,
                    help="version of the other mod; only used to name the generated patch file")
    ap.add_argument("--smooth-version", required=True,
                    help="the Smooth UI Remastered release that the patches target, e.g. 0.3. A patch is built for "
                         "each of its builds (120 and 60 fps), with the files patched as in that build")
    ap.add_argument("--only-fps", type=int, choices=(120, 60), default=None,
                    help="build only the patch for this build of the release; for testing (releases need both). "
                         "Needed for --install and --launch")
    ap.add_argument("--overrides", type=Path, default=None,
                    help="optional file with settings to add for this mod, on top of those the Smooth UI "
                         "release was patched with (see config/compat_overrides_example.txt)")

    ap.add_argument("--install", action="store_true",
                    help="install the new mod into the game folder on success")
    ap.add_argument("--launch", action="store_true",
                    help="launch the game after build and install success")
    ap.add_argument("--strict", action=argparse.BooleanOptionalAction, default=True,
                    help="fail unless every file is patched exactly as configured: no file failed or left "
                         "unchanged, every explicit rule followed, and no unreviewed warnings (default: on; "
                         "use --no-strict to build anyway, e.g. while experimenting)")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    ap.add_argument("--force", action="store_true",
                    help="build for a --smooth-version older than the most recent release in manifests/")

    # Dev hooks; useful for exploring the data format, etc.
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dev-dump", metavar="FILE",
                   help="DEV: target a specific .redswf FILE, write its decompressed body to "
                        "{FILE}_payload.swf, and exit")
    g.add_argument("--dev-insert", metavar="FILE",
                   help="DEV: target a specific .redswf FILE, and replace its body with the "
                        "(manually edited) contents of {FILE}_payload.swf")
    g.add_argument("--dev-pause", metavar="FILE",
                   help="DEV: target a specific .redswf FILE, dump its body to {FILE}_payload.swf, "
                        "pause until you type 'continue', then use the edited file")

    args = ap.parse_args(argv)
    if args.only_fps is None:
        for option, given in (("--install", args.install), ("--launch", args.launch)):
            if given:
                ap.error(f"{option} needs --only-fps, to pick the patch for the Smooth UI Remastered build you "
                         "have installed")
    return args

def mod_folder_name(name):
    return f"mod000_{MAIN_MOD_NAME}_{name}_Compat"

def check_member_paths(names, dest_dir):
    """Refuse archives containing paths that would extract outside dest_dir"""
    root = dest_dir.resolve()
    for name in names:
        target = (root / name).resolve()

        if target != root and root not in target.parents:
            fail(f"unsafe path in archive: {name}")

def unpack(mod_archive, dest_dir):
    """Extract the whole mod archive (.zip, .7z or .rar) into dest_dir"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        if zipfile.is_zipfile(mod_archive):
            with zipfile.ZipFile(mod_archive) as z:
                check_member_paths(z.namelist(), dest_dir)
                z.extractall(dest_dir)
        elif py7zr.is_7zfile(mod_archive):
            with py7zr.SevenZipFile(mod_archive) as z:
                check_member_paths(z.getnames(), dest_dir)
                z.extractall(path=dest_dir)
        elif rarfile.is_rarfile(mod_archive):
            with rarfile.RarFile(mod_archive) as z:
                check_member_paths(z.namelist(), dest_dir)
                z.extractall(dest_dir)
        else:
            fail(f"not a supported archive (.zip, .7z, .rar): {mod_archive}")
    except rarfile.RarCannotExec:
        fail("unpacking .rar files needs an external tool: install UnRAR, unar or bsdtar "
             "and make sure it is on your PATH. See README.md for more information.")
    except (zipfile.BadZipFile, py7zr.Bad7zFile, rarfile.Error) as e:
        fail(f"could not unpack {mod_archive}: {e}")
    count = sum(1 for f in dest_dir.rglob("*") if f.is_file())
    print(f"Unpacked {count} files into {dest_dir}")

def is_dlc_dir(rel):
    """Whether a folder holding bundles (relative to the unpacked archive) is DLC content: anything
    under a dlc folder, or a dlcXxx/content folder shipped without the dlc folder around it"""
    return "dlc" in (part.lower() for part in rel.parts) or rel.parent.name.lower().startswith("dlc")

def find_content_dir(unpacked_dir):
    """The folder that holds the mod's bundle(s).

    DLC bundles are skipped, like the mod's loose files: they never conflict with the files
    Smooth UI Remastered patches, so only the bundles in a mod folder matter."""
    content_dirs = sorted({b.parent for b in unpacked_dir.rglob("*.bundle")})
    if not content_dirs:
        fail("no .bundle files found in the .zip (mods shipping loose files are not supported)")

    dlc_dirs = [d for d in content_dirs if is_dlc_dir(d.relative_to(unpacked_dir))]
    for d in dlc_dirs:
        print(f"  ignored: {d.relative_to(unpacked_dir)} (DLC bundles can't conflict with Smooth UI Remastered)")
    content_dirs = [d for d in content_dirs if d not in dlc_dirs]

    if not content_dirs:
        fail("only DLC bundles found, none in a mod folder. This mod does not need a compat patch.")
    if len(content_dirs) > 1:
        # Usually a mod with optional parts. One patch covering every part would override the files
        # of parts the user didn't install, and parts may ship different versions of the same file.
        listing = "\n".join(f"  {d.relative_to(unpacked_dir)}" for d in content_dirs)
        fail(f"bundles found in several mod folders, this is not supported:\n{listing}\n"
             "If these are optional parts, build from an archive holding only the part to support.")
    return content_dirs[0]

def unbundle(content_dir, out_dir):
    """Extract the bundle(s) in content_dir into loose files under out_dir, at their depot paths"""
    entries = {}    # lowercase path -> (bundle name, entry)
    try:
        for bundle in sorted(content_dir.glob("*.bundle")):
            for entry in read_bundle(bundle.read_bytes()):
                key = entry.path.lower()
                if key in entries and entries[key][1].content() != entry.content():
                    # Which copy the game would use is unknown
                    fail(f"{entry.path} is in both {entries[key][0]} and {bundle.name}, with different contents")
                entries[key] = (bundle.name, entry)
        written = extract((entry for _, entry in entries.values()), out_dir)
    except BundleError as e:
        fail(f"can't unbundle {content_dir}: {e}")
    print(f"Extracted {len(written)} files into {out_dir}")
    if not any(out_dir.rglob("*.redswf")):
        fail(f"no .redswf files found after unbundling into {out_dir}")

def load_release_manifest(smooth_version, fps):
    """The manifest of one build of a Smooth UI release (smooth_version without a frame rate); fails
    unless it is one compat patches can be built from"""
    version = build_version(smooth_version, fps)
    manifest = load_manifest(version)
    too_old = (f"Smooth UI Remastered {smooth_version} is from before the 60 and 120 fps builds, which this "
               "patcher can't reproduce. Compat patches can be built for v0.3 and later releases")
    if manifest is None:
        fail(too_old if load_manifest(smooth_version) else f"no manifest exists for Smooth UI Remastered {version}")
    if "fps" not in manifest:
        fail(too_old)
    return manifest

def check_not_outdated(smooth_version, force):
    """Fail if smooth_version (without a frame rate specified) is older than the most recent release with a
    manifest, unless force. Dev versions (e.g. v0.4dev) don't count as releases."""
    releases = {split_build_version(path.stem)[0] for path in MANIFEST_DIR.glob("*.json")}
    releases = [version for version in releases
                if (key := version_key(version)) is not None and key[1] != SUFFIXES.index("dev")]
    newest = max(releases, key=version_key, default=None)
    if newest is None or version_key(smooth_version) is None or version_key(smooth_version) >= version_key(newest):
        return
    if not force:
        fail(f"--smooth-version {smooth_version} is older than {newest}, the most recent release in "
             f"{MANIFEST_DIR}. Use --force to build for it anyway")
    print(f"{Style.BRIGHT}{Fore.YELLOW}Warning: building for {smooth_version}, which is older than {newest}, "
          f"the most recent release (--force){Style.RESET_ALL}")

def profile_from_manifest(manifest):
    """The settings each file was patched with in a Smooth UI release, from its manifest (see
    load_release_manifest): a profile ({profile key: PatchSettings}), so compat patches match the release."""
    profile = {}
    for key, record in manifest["files"].items():
        report = record["report"]
        profile[key] = PatchSettings(record["fps"],
                                     tuple(SpeedRule(rule["pattern"], parse_speed(rule["speed"])) for rule in report["rules"]),
                                     _recorded_tokens(report, "include"), _recorded_tokens(report, "exclude"),
                                     _recorded_fixes(key, report))
    return profile

def _recorded_fixes(key, report):
    """The names of the fixes a release applied to a file"""
    names = tuple(fix["name"] for fix in report["fixes"])
    for name in names:
        try:
            check_fix_name(name)
        except ValueError as e:
            fail(f"{key}: the release's manifest names {e}; this version of the patcher can't reproduce it")
    return names

def _recorded_tokens(report, kind):
    """A report's include or exclude patterns, with sprite ids expecting the class and frame count
    recorded for them"""
    recorded_sprites = {sprite["id"]: (sprite["class"], sprite["frames"]) for sprite in report["sprites"]}
    parsed = (parse_sprite_pattern(setting["pattern"]) for setting in report[kind])
    return tuple(SpriteRef(token.id, recorded_sprites.get(token.id)) if isinstance(token, SpriteRef)
                 else token for token in parsed)

def find_shared_files(their_dir, manifest, smooth_version):
    """The .redswf files (paths relative to their_dir, in their spelling) that the other mod
    ships AND that the Smooth UI Remastered release named by manifest patches"""
    def key(rel):
        """One comparable spelling for both sides: lowercase, slash-separated"""
        return rel.as_posix().lower() if isinstance(rel, Path) else rel.lower()

    our_keys = {key(rel) for rel in manifest["files"]}
    theirs = sorted(f.relative_to(their_dir) for f in their_dir.rglob("*.redswf"))
    shared = [p for p in theirs if key(p) in our_keys]

    print(f"The other mod has {len(theirs)} .redswf files, {len(shared)} of which "
          f"Smooth UI Remastered {smooth_version} also patches")
    for p in theirs:
        if key(p) not in our_keys:
            print(f"  not patched: {p} (not in our mod)")

    if not shared:
        fail("no .redswf files in common! This mod does not need a compat patch.")
    return shared

def main():
    args = parse_args()

    # Dev mode: hand the mode to the patcher's dev hook
    if args.dev_dump:
        dev_mode.set_dev("dump", args.dev_dump)
    elif args.dev_insert:
        dev_mode.set_dev("insert", args.dev_insert)
    elif args.dev_pause:
        dev_mode.set_dev("pause", args.dev_pause)

    if not args.mod_archive.is_file():
        fail(f"input mod archive not found: {args.mod_archive}")

    paths = config_paths(load_config())
    game_path = paths.get("game_path")

    mod_name = mod_folder_name(args.mod_name)
    mod_version = normalize_version(args.mod_version)
    smooth_version = normalize_version(args.smooth_version)
    if split_build_version(smooth_version)[1] is not None:
        fail(f"--smooth-version {args.smooth_version}: give the release without a frame rate (e.g. "
             f"{split_build_version(smooth_version)[0]}); a patch is built for each of its builds "
             "(use --only-fps for just one)")
    fps_list = (args.only_fps,) if args.only_fps else (120, 60)
    # Checked first, so that a wrong version fails before the slow steps
    check_not_outdated(smooth_version, args.force)
    manifests = {fps: load_release_manifest(smooth_version, fps) for fps in fps_list}

    work_dir = paths["working_dir"] / "compat"
    unpacked_dir = work_dir / "1_unpacked"
    unbundled_dir = work_dir / "2_unbundled"

    # Perform some sanity checks, since every user that didn't change the config will likely get a very, very ugly exception tree otherwise
    if game_path and not game_path.is_dir():
        fail(f"game_path is not set to a valid directory in config/config.toml: {game_path} is not an existing directory")
    if not (parent := work_dir.parent.parent).is_dir():
        fail(f"working directory parent {parent} does not exist")
    if not paths["wcc_lite"].is_file():
        fail(f"wcc_lite setting in config/config.toml is not set to a valid wcc_lite.exe path: {paths['wcc_lite']} does not exist")

    print(f"Mod folder: {mod_name}")

    print_step_header(f"Cleaning {work_dir}")
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    print_step_header(f"Unpacking {args.mod_archive}")
    unpack(args.mod_archive, unpacked_dir)

    print_step_header("Unbundling the other mod")
    unbundle(find_content_dir(unpacked_dir), unbundled_dir)

    # The patches are made one after the other; a failure stops the run, leaving the earlier ones as they are
    versions = [build_version(smooth_version, fps) for fps in fps_list]
    issue_printers = []
    for version, fps in zip(versions, fps_list):
        zip_out = REPO_ROOT / f"SmoothUIRemastered_{version}_{args.mod_name}_{mod_version}_Compat.zip"
        print_step_header(f"Compat patch for Smooth UI Remastered {version}: {zip_out.name}")
        patched_dir = work_dir / f"{fps}fps" / "3_patched"
        mod_root = work_dir / f"{fps}fps" / "4_mod"
        content_dir = mod_root / mod_name / "content"
        content_dir.mkdir(parents=True)

        print_issues, patched = make_patch(version, manifests[fps], unbundled_dir, patched_dir, args)
        issue_printers.append(print_issues)
        pack_content(patched_dir, content_dir)
        zip_tree(mod_root, zip_out)
        # Builds of one frame rate are for testing, and dev hooks change the files by hand
        if not (args.only_fps or args.dev_dump or args.dev_insert or args.dev_pause):
            write_patch_manifest(version, manifests[fps], args, mod_version, zip_out,
                                 unbundled_dir, patched_dir, patched)

    if args.only_fps:
        install_and_launch(mod_root / mod_name, game_path, args.install, args.launch)

    finish_work_dir(work_dir, args.keep_work)

    # Repeated for every patch, as the output of the builds has likely scrolled the issues away
    for version, print_issues in zip(versions, issue_printers):
        print_step_header(f"Warnings and errors: {version}")
        print_issues()
    print(f"\nCreated {len(versions)} compat patch(es): " + ", ".join(versions))

def make_patch(version, manifest, unbundled_dir, patched_dir, args):
    """Patch the files the other mod shares with one build of the release (version, e.g. v0.3-60fps) into
    patched_dir, and check the result. Returns a function that prints the warnings and rule issues."""
    print_step_header(f"Finding files in common with Smooth UI Remastered {version}")
    print(f"Files are patched to {manifest['fps']} fps, as in that build")
    shared = find_shared_files(unbundled_dir, manifest, version)
    shared_keys = {rel.as_posix().lower() for rel in shared}
    shared_profile = {key: settings for key, settings in profile_from_manifest(manifest).items()
                      if key in shared_keys}
    shared_settings, reviewed = [], set()
    if args.overrides:
        shared_profile, shared_settings, reviewed = apply_overrides(shared_profile, args.overrides)

    print_step_header(f"Running FPS patcher ({version})")

    # The release's reports; warnings it had for a file were already there in the vanilla version.
    # They stay hidden where the other mod's code is the same (see reviewed_warnings.py).
    recorded = {key: record["report"] for key, record in manifest["files"].items()}
    review = WarningReview(reviewed, {key: {review_of(warning) for warning in report["warnings"]}
                                      for key, report in recorded.items()})
    try:
        patched, kept, failed = patcher_core.patch_files(
            unbundled_dir, patched_dir, shared_profile,
            on_file=partial(print_file_result, is_reviewed=review.is_reviewed))
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done ({version}): {len(patched)} patched, {len(kept)} left alone, {len(failed)} failed.")

    warnings = review.summarize([*patched, *kept], unchecked=len(failed))
    how_to_accept = [("These come from the other mod's changes. Once checked, add these lines under * in "
                      "the --overrides file to mark them as reviewed:"),
                     *(f"    reviewed: {review}" for review in warnings.unreviewed)]
    print_warnings = partial(print_warning_summary, warnings, how_to_accept=how_to_accept,
                             reviewed_source=str(args.overrides))
    print_warnings()
    rule_issues = patcher_core.find_rule_issues(patched, kept, shared_settings, recorded)
    print_rule_issues(rule_issues)
    if args.strict:
        enforce_strict(failed, kept, rule_issues.errors, warnings.unreviewed)

    # A conflicting file missing from the patch would let our main mod's version
    # override the other mod's changes to that screen.
    if failed:
        fail(f"{len(failed)} conflicting file(s) could not be patched, see above")
    if not patched:
        fail("no patched files were produced, aborting.")

    def print_issues():
        print_warnings()
        print_rule_issues(rule_issues)
    return print_issues, patched

def write_patch_manifest(version, manifest, args, mod_version, zip_out, unbundled_dir, patched_dir, patched):
    """Write manifests/compat/<version>_<mod>_<mod version>.json for a patch built for one build of the
    release (version, e.g. v0.3-60fps; manifest is its manifest), replacing any earlier one: what the
    patch holds, and the release's own output for each of those files. A later release that patches a
    file differently (other settings, fixes or patcher code) makes the patch outdated; so does one that
    starts patching a file the other mod ships. See manifest_diff.py's compat_patch_problems."""
    print_step_header(f"Writing compat patch manifest ({version})")
    release_files = {profile_key(key): record for key, record in manifest["files"].items()}
    files = {}
    for entry in sorted(patched, key=lambda e: e["path"]):
        rel = entry["path"]
        files[rel.as_posix()] = {
            "source_swf_sha256": swf_sha256((unbundled_dir / rel).read_bytes()),
            "output_swf_sha256": swf_sha256((patched_dir / rel).read_bytes()),
            "release_output_swf_sha256": release_files[profile_key(rel)]["output_swf_sha256"],
        }
    commit, uncommitted = patcher_commit()
    path = compat_manifest_path(version, args.mod_name, mod_version)
    write_json(path, {
        "mod_name": args.mod_name,
        "mod_version": mod_version,
        "smooth_version": version,
        "fps": manifest["fps"],
        # The release's manifest is rewritten when it is rebuilt; this tells which build the patch is from
        "release_created_at": manifest.get("created_at"),
        "overrides": str(args.overrides) if args.overrides else None,
        "patcher_commit": commit,
        "patcher_uncommitted_changes": uncommitted,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "output_zip": str(zip_out.relative_to(REPO_ROOT)) if zip_out.is_relative_to(REPO_ROOT) else str(zip_out),
        # Every .redswf the other mod ships, patched or not
        "other_mod_files": sorted(f.relative_to(unbundled_dir).as_posix() for f in unbundled_dir.rglob("*.redswf")),
        "files": files,
    })
    print(f"Wrote compat patch manifest {path} ({len(files)} files)")
