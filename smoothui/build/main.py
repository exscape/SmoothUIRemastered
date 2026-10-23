#!/usr/bin/env python3

# build-main.py -- patch UI frame rates, then pack the mod.
# The files to patch come from the profile file (see config/). A release (--make-release) is one build
# per release frame rate (120 and 60 fps), each with its manifest, and ends by listing the compat patches
# it makes outdated (see compat_status.py); other builds are at --fps.

import argparse
import datetime
import logging
import re
import shutil
from functools import partial

from ..common import fail
from ..log import HEADING
from ..patcher import dev_mode, patcher_core
from ..witcher3.source_file import GAME_FILES, SourceFile
from .compat_status import print_compat_status
from .game_files import game_file_report
from .manifest_diff import print_changes_since_previous
from .profile import load_profile
from .reviewed_warnings import (
    WarningReview,
    accept_warnings,
    load_reviewed,
    print_warning_summary,
)
from .shared import (
    REPO_ROOT,
    add_mod_files,
    build_version,
    config_paths,
    enforce_strict,
    finish_work_dir,
    install_and_launch,
    latest_manifests,
    load_config,
    manifest_path,
    pack_content,
    patcher_commit,
    print_file_result,
    print_rule_issues,
    write_build_record,
    write_manifest,
    write_patched_files,
    zip_tree,
)
from .strings import build_strings, check_tool

log = logging.getLogger(__name__)

OUTPUT_MOD_NAME = "modSmoothUIRemastered"
DEFAULT_FPS = 120
# A game version at the end of a folder name: 5.01, 5.00c
GAME_VERSION_SUFFIX = re.compile(r"\d+(?:\.\d+)+[a-z]?$")

def parse_args():
    ap = argparse.ArgumentParser(
        description="Patch vanilla UI frame rates, then create a mod .zip: one build at --fps (default 120), "
                    "or every release build with --make-release")

    ap.add_argument("--make-release", action="store_true",
                    help="build the release: the 120 and 60 fps builds, each with its manifest")
    ap.add_argument("--fps", type=int, default=None,
                    help=f"build only at this frame rate (default: {DEFAULT_FPS}); for development, so not "
                         "with --make-release")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be patched, without writing or packing anything")
    ap.add_argument("--profile", default=None,
                    help="the profile to use (default: config/profile_default.txt); it specifies which files "
                         "to patch, and how")
    ap.add_argument("--install", action="store_true",
                    help="install the mod into the game folder on success (not with --make-release: build the "
                         "frame rate to test with --fps)")
    ap.add_argument("--launch", action="store_true",
                    help="launch the game after build and install success")
    ap.add_argument("--strict", action=argparse.BooleanOptionalAction, default=True,
                    help="fail unless every file is patched exactly as configured: no file left unchanged, "
                         "every explicit rule followed, and no unreviewed warnings (default: on; use --no-strict "
                         "to build anyway, e.g. while experimenting). A file that fails always fails the build")
    ap.add_argument("--accept-warnings", action="store_true",
                    help="mark all current patcher warnings as reviewed (adds them to config/reviewed_warnings.txt); "
                         "only after checking them by hand")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    ap.add_argument("--force", action="store_true",
                    help="with --make-release: release this version even though it already seems to exist "
                         "(overwrites its manifests)")

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

    args = ap.parse_args()
    if args.make_release:
        for option, given in (("--fps", args.fps is not None), ("--install", args.install), ("--launch", args.launch),
                              ("--dev-*", args.dev_dump or args.dev_insert or args.dev_pause)):
            if given:
                ap.error(f"{option} is for single builds; --make-release builds every release frame rate")
    if args.fps is None:
        args.fps = DEFAULT_FPS
    if not 1 <= args.fps <= 255:
        ap.error(f"--fps {args.fps} is outside 1-255")
    return args

def check_game_version(uncooked_files, targeted_game_version):
    """Fail if the uncooked files' folder name ends with a game version (RemasteredUncooked5.00c) that is
    not targeted_game_version. Other names (RemasteredUncooked) say nothing, so they pass."""
    match = GAME_VERSION_SUFFIX.search(uncooked_files.name)     # .name ignores trailing slashes
    if match and match.group() != targeted_game_version:
        fail(f"uncooked_files is for game version {match.group()} ({uncooked_files}), but targeted_game_version "
             f"in the [versions] section of the config file is {targeted_game_version!r}")

def main():
    args = parse_args()

    # Dev mode: hand the mode to the patcher's dev hook
    if args.dev_dump:
        dev_mode.set_dev("dump", args.dev_dump)
    elif args.dev_insert:
        dev_mode.set_dev("insert", args.dev_insert)
    elif args.dev_pause:
        dev_mode.set_dev("pause", args.dev_pause)

    config = load_config()
    paths = config_paths(config)

    base_version = config['versions']['created_mod_version']
    if not base_version or not base_version.startswith("v"):
        fail("Invalid created_mod_version in [versions] section of config file")
    check_game_version(paths['uncooked_files'], config['versions'].get('targeted_game_version'))
    fps_list = (120, 60) if args.make_release else (args.fps,)
    versions = [build_version(base_version, fps) for fps in fps_list]

    if args.make_release and not args.dry_run:
        existing = [version for version in versions if manifest_path(version).exists()]
        if existing and args.force:
            log.info(f"Overwriting previously existing mod release(s) {', '.join(existing)}, including the manifests")
        elif existing:
            fail(f"Version(s) {', '.join(existing)} already seem to exist! Use --force to overwrite, "
                 "including the manifests")

    if not paths['uncooked_files'].is_dir():
        fail(f"not a directory: {paths['uncooked_files']}")

    # Before building: a release build replaces the latest manifest with one made from these very files
    game_file_text, game_files_changed = "", 0
    if latest := latest_manifests():
        log.info(f"Game files against {', '.join(path.stem for path in latest.values())}", extra=HEADING)
        game_file_text, game_files_changed = game_file_report(paths['uncooked_files'], latest.values())
        log.log(logging.WARNING if game_files_changed else logging.INFO, game_file_text)

    # The settings menu's strings, which every build ships (from assets/mod_files/; see strings.py)
    if not args.dry_run:
        check_tool(paths.get('w3stringsr'))
    build_strings(paths.get('w3stringsr'), paths['working_dir'] / "strings", OUTPUT_MOD_NAME, dry_run=args.dry_run)

    # The builds run one after the other; a failure stops the run, leaving the earlier builds as they are
    issue_printers = [build(args, config, paths, version, fps) for version, fps in zip(versions, fps_list)]

    # Repeated for every build, as the output of the builds has likely scrolled the issues away
    for version, print_issues in zip(versions, issue_printers):
        log.info(f"Warnings and errors: {version}", extra=HEADING)
        print_issues()
    if game_files_changed:
        log.info("Game files changed since the latest builds (checked before building)", extra=HEADING)
        log.warning(game_file_text)

    if args.make_release and not args.dry_run:
        for version in versions:
            print_compat_status(version)

    if args.dry_run:
        log.info("DRY RUN: exiting", extra=HEADING)

def read_game_files(uncooked_files, profile):
    """The files the profile patches, as SourceFiles in the uncooked game files"""
    inputs = []
    for path in profile:
        try:
            inputs.append(SourceFile.loose(path, GAME_FILES, uncooked_files / path))
        except OSError as e:
            fail(f"can't read {path} from the uncooked files: {e}")
    return inputs

def build(args, config, paths, version, fps):
    """Patch, check, pack and zip one build (and for a release, write its manifest). Returns a function
    that prints the build's warnings and rule issues."""
    log.info(f"Building {version}: every file patched to {fps} fps", extra=HEADING)
    profile, shared_settings = load_profile(args.profile, fps)
    work_dir = paths['working_dir'] / f"main_{fps}fps"
    patched_dir = work_dir / "1_patched"
    output_dir = work_dir / "2_mod"     # laid out as the zip: mods/<the mod>/, bin/
    output_mod_dir = output_dir / "mods" / OUTPUT_MOD_NAME
    output_content_path = output_mod_dir / "content"
    zip_out = REPO_ROOT / f"SmoothUIRemastered_{version}.zip"

    if not args.dry_run:
        # Clean out stale output
        log.info(f"Cleaning {patched_dir}", extra=HEADING)
        shutil.rmtree(patched_dir, ignore_errors=True)
        output_content_path.mkdir(parents=True, exist_ok=True)

    log.info(f"Running FPS patcher ({version})", extra=HEADING)
    review = WarningReview(load_reviewed())
    inputs = read_game_files(paths['uncooked_files'], profile)
    try:
        patched, kept, failed = patcher_core.patch_files(
            inputs, profile, on_file=partial(print_file_result, is_reviewed=review.is_reviewed))
    except ValueError as e:
        fail(str(e))
    log.info(f"\nDone ({version}): {len(patched)} patched, {len(kept)} left alone, {len(failed)} failed.")
    if not args.dry_run:
        write_patched_files(patched, patched_dir)

    warnings = review.summarize([*patched, *kept], unchecked=len(failed))
    if args.accept_warnings and warnings.unreviewed:
        accept_warnings(warnings.unreviewed)
        warnings = WarningReview(load_reviewed()).summarize([*patched, *kept], unchecked=len(failed))
    print_warnings = partial(print_warning_summary, warnings, reviewed_source="config/reviewed_warnings.txt",
                             how_to_accept=["Once checked, run again with --accept-warnings to mark them as reviewed."])
    print_warnings()
    rule_issues = patcher_core.find_rule_issues(patched, kept, shared_settings)
    print_rule_issues(rule_issues)
    if failed:
        fail(f"{len(failed)} file(s) could not be patched, see above")
    if args.strict:
        enforce_strict(kept, rule_issues.errors, warnings.unreviewed)

    def print_issues():
        print_warnings()
        print_rule_issues(rule_issues)

    if args.dry_run:
        return print_issues
    if not patched:
        fail("no patched files were produced, aborting.")

    log.info(f"Removing the old output folder ({version})", extra=HEADING)
    shutil.rmtree(output_dir, ignore_errors=True)
    if output_dir.exists():
        fail(f"could not remove the old output folder {output_dir}")
    output_content_path.mkdir(parents=True)

    pack_content(patched, output_content_path)
    add_mod_files(output_dir, OUTPUT_MOD_NAME)
    write_build_record(output_mod_dir, version, fps)

    zip_tree(output_dir, zip_out)

    if args.make_release:
        log.info(f"Writing mod manifest ({version})", extra=HEADING)
        # Hashes and sizes of the source and output files (for v0.2, source_size == output_size for all files)
        records = {entry["path"]: {**entry["hashes"], "fps": entry["fps"], "report": entry["report"]}
                   for entry in sorted(patched, key=lambda e: e["path"])}
        commit, uncommitted = patcher_commit()
        write_manifest(version, records, extra={
            "fps": fps,
            "patcher_commit": commit,
            "patcher_uncommitted_changes": uncommitted,
            "targeted_game_version": config['versions'].get('targeted_game_version'),
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "output_zip": str(zip_out.relative_to(REPO_ROOT)) if zip_out.is_relative_to(REPO_ROOT) else str(zip_out),
        })
        print_changes_since_previous(version)
    else:
        install_and_launch(output_mod_dir, paths['game_path'], args.install, args.launch,
                           bin_dir=output_dir / "bin")

    finish_work_dir(work_dir, args.keep_work)
    return print_issues
