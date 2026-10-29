#!/usr/bin/env python3

# build-compat.py -- take an existing mod .zip/.rar/.7z, apply framerate patches to the .redswf files
# that conflict with Smooth UI Remastered, and build compatibility patches containing only those
# patched files: one for each build of the Smooth UI release (120 and 60 fps), unless --only-fps is specified for development/testing.
#
# The files to patch and their settings come from the release's manifests (see manifests/), selected by
# --smooth-version, optionally with additions from an override file (--overrides; see
# config/compat_overrides_example.txt).
# Unless --only-fps or a dev hook is given, each patch also gets a manifest in manifests/compat/.
# build-main.py and version-tool.py use these to tell which compat patches a new release makes outdated.
# Each patch is also compared with the mod's newest earlier patch, with a warning if it is identical (not needed).
# Example usage: python build-compat.py modOtherUI.zip --mod-name OtherMod --mod-version 1.0 --smooth-version 0.3

import argparse
import datetime
import logging
import shutil
import zipfile
from functools import partial
from pathlib import Path

import py7zr
import rarfile

from ..common import fail, normalize_version, profile_key
from ..log import HEADING
from ..manifests import shipped_files
from ..patcher import dev_mode
from ..witcher3 import mod_bundles
from ..witcher3.mod_bundles import list_bundles
from ..witcher3.source_file import SourceFile
from .compat_status import newest_compat_patches
from .profile import apply_overrides
from .release import load_release_manifest, patch_as_release, profile_from_manifest
from .reviewed_warnings import print_warning_summary
from .shared import (
    REPO_ROOT,
    build_version,
    compat_manifest_path,
    config_paths,
    enforce_strict,
    finish_work_dir,
    install_and_launch,
    is_dev_version,
    load_config,
    pack_content,
    patcher_commit,
    print_rule_issues,
    release_manifest_paths,
    split_build_version,
    version_key,
    write_json,
    write_patched_files,
    zip_tree,
)

log = logging.getLogger(__name__)


# rarfile looks for an external unrar in PATH and in the current directory. When neither has it,
# point it at our UnRAR.exe (assets/unrar/ in the repo), so .rar mods work from any working directory.
def setup_rar_tool():
    if shutil.which(rarfile.UNRAR_TOOL):
        return # a system unrar is preferred (e.g. under WSL)
    bundled_unrar = REPO_ROOT / "assets" / "unrar" / "UnRAR.exe"
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
                    help="fail unless every file is patched exactly as configured: no file left unchanged, "
                         "every explicit rule followed, and no unreviewed warnings (default: on; use --no-strict "
                         "to build anyway, e.g. while experimenting). A file that fails always fails the build")
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
    log.info(f"Unpacked {count} files into {dest_dir}")

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
        log.info(f"  ignored: {d.relative_to(unpacked_dir)} (DLC bundles can't conflict with Smooth UI Remastered)")
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

def read_bundle_tocs(content_dir):
    """{profile key: (bundle file, TocEntry)} for the files in the bundle(s) in content_dir (see
    mod_bundles.read_bundle_tocs); fails unless they hold a .redswf file"""
    files = mod_bundles.read_bundle_tocs(content_dir)
    log.info(f"Found {len(files)} files in {len(list_bundles(content_dir))} bundle(s)")
    if not any(key.endswith(".redswf") for key in files):
        fail(f"no .redswf files found in the bundles in {content_dir}")
    return files

def their_swf_paths(their_files):
    """The other mod's .redswf files (read_bundle_tocs' result), / separated, in their spelling"""
    return sorted(entry.path.replace("\\", "/") for _, entry in their_files.values()
                  if entry.path.lower().endswith(".redswf"))

def check_not_outdated(smooth_version, force):
    """Fail if smooth_version (without a frame rate specified) is older than the most recent release with a
    manifest, unless force. Dev versions (e.g. v0.4dev) don't count as releases."""
    releases = {split_build_version(version)[0] for version in release_manifest_paths()}
    releases = [version for version in releases if version_key(version) is not None and not is_dev_version(version)]
    newest = max(releases, key=version_key, default=None)
    if newest is None or version_key(smooth_version) is None or version_key(smooth_version) >= version_key(newest):
        return
    if not force:
        fail(f"--smooth-version {smooth_version} is older than {newest}, the most recent release with a "
             "manifest. Use --force to build for it anyway")
    log.warning(f"building for {smooth_version}, which is older than {newest}, the most recent release (--force)")

def find_shared_files(theirs, manifest, smooth_version):
    """The .redswf files (their_swf_paths, in their spelling) that the other mod ships AND that the
    Smooth UI Remastered release named by manifest patches and ships (the merger alone builds the others)"""
    our_keys = {profile_key(rel) for rel in shipped_files(manifest)}
    shared = [p for p in theirs if profile_key(p) in our_keys]

    log.info(f"The other mod has {len(theirs)} .redswf files, {len(shared)} of which "
             f"Smooth UI Remastered {smooth_version} also patches")
    for p in theirs:
        if profile_key(p) not in our_keys:
            log.info(f"  not patched: {p} (not in our mod)")

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

    # Perform some sanity checks, since every user that didn't change the config will likely get a very, very ugly exception tree otherwise
    if game_path and not game_path.is_dir():
        fail(f"game_path is not set to a valid directory in config/config.toml: {game_path} is not an existing directory")
    if not (parent := work_dir.parent.parent).is_dir():
        fail(f"working directory parent {parent} does not exist")

    log.info(f"Mod folder: {mod_name}")

    log.info(f"Cleaning {work_dir}", extra=HEADING)
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    log.info(f"Unpacking {args.mod_archive}", extra=HEADING)
    unpack(args.mod_archive, unpacked_dir)

    log.info("Reading the other mod's bundles", extra=HEADING)
    their_content_dir = find_content_dir(unpacked_dir)
    their_files = read_bundle_tocs(their_content_dir)

    # The patches are made one after the other; a failure stops the run, leaving the earlier ones as they are
    versions = [build_version(smooth_version, fps) for fps in fps_list]
    issue_printers, unneeded = [], []
    for version, fps in zip(versions, fps_list):
        zip_out = REPO_ROOT / f"SmoothUIRemastered_{version}_{args.mod_name}_{mod_version}_Compat.zip"
        log.info(f"Compat patch for Smooth UI Remastered {version}: {zip_out.name}", extra=HEADING)
        patched_dir = work_dir / f"{fps}fps" / "2_patched"
        mod_root = work_dir / f"{fps}fps" / "3_mod"
        content_dir = mod_root / mod_name / "content"
        content_dir.mkdir(parents=True)

        print_issues, patched = make_patch(version, manifests[fps], their_files, their_content_dir.parent.name,
                                           patched_dir, args)
        issue_printers.append(print_issues)
        pack_content(patched, content_dir)
        zip_tree(mod_root, zip_out)
        # Builds of one frame rate are for testing, and dev hooks change the files by hand
        if not (args.only_fps or args.dev_dump or args.dev_insert or args.dev_pause):
            unneeded += write_patch_manifest(version, manifests[fps], args, mod_version, zip_out,
                                             their_files, patched)

    if args.only_fps:
        install_and_launch(mod_root / mod_name, game_path, args.install, args.launch)

    finish_work_dir(work_dir, args.keep_work)

    # Repeated for every patch, as the output of the builds has likely scrolled the issues away
    for version, print_issues in zip(versions, issue_printers):
        log.info(f"Warnings and errors: {version}", extra=HEADING)
        print_issues()
    log.info(f"\nCreated {len(versions)} compat patch(es): " + ", ".join(versions))
    for warning in unneeded:
        log.warning(warning)

def make_patch(version, manifest, their_files, origin, patched_dir, args):
    """Patch the files the other mod (their_files: see read_bundle_tocs; origin: its folder) shares with
    one build of the release (version, e.g. v0.3-60fps), writing them to patched_dir too, and check the
    result. Returns (a function that prints the warnings and rule issues, patch_files' patched list)."""
    log.info(f"Finding files in common with Smooth UI Remastered {version}", extra=HEADING)
    log.info(f"Files are patched to {manifest['fps']} fps, as in that build")
    shared = find_shared_files(their_swf_paths(their_files), manifest, version)
    inputs = [SourceFile.in_bundle(profile_key(p), origin, *their_files[profile_key(p)]) for p in shared]
    shared_keys = {source.path for source in inputs}
    shared_profile = {key: settings for key, settings in profile_from_manifest(manifest).items()
                      if key in shared_keys}
    shared_settings, reviewed = [], set()
    if args.overrides:
        shared_profile, shared_settings, reviewed = apply_overrides(shared_profile, args.overrides)

    log.info(f"Running FPS patcher ({version})", extra=HEADING)
    # (No mod_reviews: a compat patch is built for one known mod, so nothing is known broken here)
    patched, kept, failed, warnings, rule_issues, _ = patch_as_release(
        inputs, shared_profile, manifest, reviewed=reviewed, shared_settings=shared_settings)
    log.info(f"\nDone ({version}): {len(patched)} patched, {len(kept)} left alone, {len(failed)} failed.")
    write_patched_files(patched, patched_dir)

    how_to_accept = [("These come from the other mod's changes. Once checked, add these lines under * in "
                      "the --overrides file to mark them as reviewed:"),
                     *(f"    reviewed: {review}" for review in warnings.unreviewed)]
    print_warnings = partial(print_warning_summary, warnings, how_to_accept=how_to_accept,
                             reviewed_source=str(args.overrides))
    print_warnings()
    print_rule_issues(rule_issues)
    # A conflicting file missing from the patch would let our main mod's version
    # override the other mod's changes to that screen.
    if failed:
        fail(f"{len(failed)} conflicting file(s) could not be patched, see above")
    if args.strict:
        enforce_strict(kept, rule_issues.errors, warnings.unreviewed)
    if not patched:
        fail("no patched files were produced, aborting.")

    def print_issues():
        print_warnings()
        print_rule_issues(rule_issues)
    return print_issues, patched

def write_patch_manifest(version, manifest, args, mod_version, zip_out, their_files, patched):
    """Write manifests/compat/<version>_<mod>_<mod version>.json for a patch built for one build of the
    release (version, e.g. v0.3-60fps; manifest is its manifest), replacing any earlier one: what the
    patch holds, and the release's own output for each of those files. A later release that patches a
    file differently (other settings, fixes or patcher code) makes the patch outdated; so does one that
    starts patching a file the other mod ships. See compat_status.py's compat_patch_problems.
    their_files and patched are as make_patch got and returned them. Returns a warning if the patch is
    identical to the mod's newest earlier one, so not needed."""
    log.info(f"Writing compat patch manifest ({version})", extra=HEADING)
    release_files = {profile_key(key): record for key, record in manifest["files"].items()}
    files = {}
    for entry in sorted(patched, key=lambda e: e["path"]):
        files[entry["path"]] = {
            "source_swf_sha256": entry["hashes"]["source_swf_sha256"],
            "output_swf_sha256": entry["hashes"]["output_swf_sha256"],
            "release_output_swf_sha256": release_files[entry["path"]]["output_swf_sha256"],
        }
    commit, uncommitted = patcher_commit()
    path = compat_manifest_path(version, args.mod_name, mod_version)
    # Identical: neither the other mod's files nor our patching of them changed
    outputs = lambda files: {profile_key(p): record["output_swf_sha256"] for p, record in files.items()}
    earlier = newest_compat_patches(version, args.mod_name, exclude=path)
    unneeded = [(f"{zip_out.name} is not needed, it is identical to {earlier[0][1]['output_zip']}. Unless you "
                 f"release it anyway, delete it and {path.relative_to(REPO_ROOT)}")] \
        if earlier and outputs(earlier[0][1]["files"]) == outputs(files) else []
    for warning in unneeded:
        log.warning(warning)
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
        "other_mod_files": their_swf_paths(their_files),
        "files": files,
    })
    log.info(f"Wrote compat patch manifest {path} ({len(files)} files)")
    return unneeded
