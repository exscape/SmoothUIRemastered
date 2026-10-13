#!/usr/bin/env python3

# build-main.py -- patch UI frame rates, then pack the mod with wcc_lite.
# The files to patch and their target frame rates come from the profile file (see config/).

import argparse
import datetime
import shutil
from functools import partial

from ..common import fail, print_step_header
from ..patcher import dev_mode, patcher_core
from ..patcher.redswf import swf_sha256
from .profile import load_profile
from .reviewed_warnings import (
    WarningReview,
    accept_warnings,
    load_reviewed,
    print_warning_summary,
)
from .shared import (
    REPO_ROOT,
    config_paths,
    enforce_strict,
    finish_work_dir,
    install_and_launch,
    load_config,
    manifest_path,
    pack_content,
    patcher_commit,
    print_file_result,
    print_rule_issues,
    resolve_zip_path,
    sha256_file,
    write_manifest,
    zip_tree,
)

OUTPUT_MOD_NAME = "modSmoothUIRemastered"

def parse_args():
    ap = argparse.ArgumentParser(
        description="Patch vanilla UI frame rates, then create a release-ready mod .zip")

    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be patched, without writing or packing anything")
    ap.add_argument("--zip", default=None,
                    help="override output .zip name or path")
    ap.add_argument("--profile", default=None,
                    help="the profile to use (default: config/profile_default.txt); it specifies which files "
                         "to patch, and how")
    ap.add_argument("--install", action="store_true",
                    help="install the mod into the game folder on success")
    ap.add_argument("--launch", action="store_true",
                    help="launch the game after build and install success")
    ap.add_argument("--strict", action=argparse.BooleanOptionalAction, default=True,
                    help="fail unless every file is patched exactly as configured: no file failed or left "
                         "unchanged, every explicit rule followed, and no unreviewed warnings (default: on; "
                         "use --no-strict to build anyway, e.g. while experimenting)")
    ap.add_argument("--accept-warnings", action="store_true",
                    help="mark all current patcher warnings as reviewed (adds them to config/reviewed_warnings.txt); "
                         "only after checking them by hand")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    ap.add_argument("--write-manifest", action="store_true",
                    help="save a manifest of this version; only intended for actual releases")
    ap.add_argument("--force", action="store_true",
                    help="create a release version even though this version already seems to exist")

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

    return ap.parse_args()

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
    profile, shared_settings = load_profile(args.profile)

    mod_version = config['versions']['created_mod_version']
    if not mod_version or not mod_version.startswith("v"):
        fail("Invalid created_mod_version in [versions] section of config file")

    if manifest_path(mod_version).exists() and args.write_manifest:
        if args.force:
            print(f"Overwriting previously existing mod release {mod_version}, including the manifest")
        else:
            fail(f"Version {mod_version} already seems to exist! Use --force to overwrite, including the manifest")

    uncooked_files = paths['uncooked_files']
    game_path = paths['game_path']
    work_dir = paths['working_dir'] / "main"

    patched_dir = work_dir / "1_patched"
    output_dir = work_dir / "2_mod"
    output_content_path = output_dir / OUTPUT_MOD_NAME / "content"

    # Path to the output .zip for distribution.
    zip_out = resolve_zip_path(args.zip, f"SmoothUIRemastered_{mod_version}.zip")

    if not uncooked_files.is_dir():
        fail(f"not a directory: {uncooked_files}")

    if not args.dry_run:
        # Clean out stale output
        print_step_header(f"Cleaning {patched_dir}")
        shutil.rmtree(patched_dir, ignore_errors=True)
        output_content_path.mkdir(parents=True, exist_ok=True)

    print_step_header("Running FPS patcher")
    review = WarningReview(load_reviewed())
    try:
        patched, kept, failed = patcher_core.patch_files(
            uncooked_files, patched_dir, profile,
            dry_run=args.dry_run, on_file=partial(print_file_result, is_reviewed=review.is_reviewed))
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done: {len(patched)} patched, {len(kept)} left alone, "
          f"{len(failed)} skipped.")

    warnings = review.summarize(patched)
    if args.accept_warnings and warnings.unreviewed:
        accept_warnings(warnings.unreviewed)
        warnings = WarningReview(load_reviewed()).summarize(patched)
    print_warnings = partial(print_warning_summary, warnings, reviewed_source="config/reviewed_warnings.txt",
                             how_to_accept=["Once checked, run again with --accept-warnings to mark them as reviewed."])
    print_warnings()
    rule_issues = patcher_core.find_rule_issues(patched, kept, shared_settings)
    print_rule_issues(rule_issues)
    if args.strict:
        enforce_strict(failed, kept, rule_issues.errors, warnings.unreviewed)

    if args.dry_run:
        print_step_header("DRY RUN: exiting")
        return

    if not patched:
        fail("no patched files were produced, aborting.")

    patched_file_list = list(patched_dir.rglob("*.redswf"))
    if len(patched) != len(patched_file_list):
        fail("BUG: number of output files doesn't match number of files patched")

    print_step_header("Removing old bundle/metadata from output folder")
    for old in [*output_content_path.glob("blob*.bundle"), output_content_path / "metadata.store"]:
        old.unlink(missing_ok=True)

    pack_content(patched_dir, output_content_path)

    zip_tree(output_dir, zip_out)

    if args.write_manifest:
        print_step_header("Writing mod manifest")
        records = {}
        for entry in sorted(patched, key=lambda e: e["path"]):
            relative_path = entry["path"]
            src = uncooked_files / relative_path
            dst = patched_dir / relative_path
            records[relative_path.as_posix()] = {
                "source_sha256": sha256_file(src),
                "output_sha256": sha256_file(dst),
                "source_swf_sha256": swf_sha256(src.read_bytes()),
                "output_swf_sha256": swf_sha256(dst.read_bytes()),
                "source_size": src.stat().st_size, # for v0.2, source_size == output_size for all files
                "output_size": dst.stat().st_size,
                "fps": entry["fps"],
                "report": entry["report"],
            }
        commit, uncommitted = patcher_commit()
        write_manifest(mod_version, records, extra={
            "patcher_commit": commit,
            "patcher_uncommitted_changes": uncommitted,
            "targeted_game_version": config['versions'].get('targeted_game_version'),
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "output_zip": str(zip_out.relative_to(REPO_ROOT)) if zip_out.is_relative_to(REPO_ROOT) else str(zip_out),
        })

    install_and_launch(output_dir / OUTPUT_MOD_NAME, game_path, args.install, args.launch)

    finish_work_dir(work_dir, args.keep_work)

    # Repeated, as the packing output has likely scrolled it away
    print_warnings()
    print_rule_issues(rule_issues)