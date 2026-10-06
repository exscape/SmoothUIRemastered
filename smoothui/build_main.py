#!/usr/bin/env python3

# build-mod.py -- patch UI frame rates, then pack the mod with wcc_lite.
# Example usage: python build-mod.py --multiplier 2 --max-fps 60 --skip-at-or-above 48
#
# Tested on Windows 11: via WSL and via cmd.exe (mostly via WSL)

import argparse
import shutil

from . import patcher
from .build_common import (
    config_paths,
    fail,
    install_mod,
    load_config,
    load_exclusions,
    print_file_result,
    print_step_header,
    resolve_zip_path,
    run_command,
    select_files,
    zip_tree,
)
from .common import to_windows
from .patcher import PatchSettings

OUTPUT_MOD_NAME = "modSmoothUIRemastered"

def parse_args():
    ap = argparse.ArgumentParser(
        description="Patch vanilla UI frame rates, then create a release-ready mod .zip")

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fps", type=float, help="set this exact frame rate")
    g.add_argument("--multiplier", type=float, help="multiply the original rate")

    ap.add_argument("--max-fps", type=float, default=120,
                    help="clamp the target rate to this (default 120)")
    ap.add_argument("--skip-at-or-above", type=float, default=60,
                    help="leave files already at/above this rate alone (default 60)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be patched, without writing or packing anything")
    ap.add_argument("--zip", default=None,
                    help="output .zip name or path (default: <mod name>.zip in the repo folder)")
    ap.add_argument("--install", action="store_true",
                    help="install the mod into the game folder on success")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    return ap.parse_args()

def main():
    args = parse_args()

    try:
        settings = PatchSettings(fps=args.fps, multiplier=args.multiplier,
                                 max_fps=args.max_fps, skip_at_or_above=args.skip_at_or_above)
    except ValueError as e:
        fail(str(e))

    paths = config_paths(load_config())

    uncooked_gameplay = paths['uncooked_gameplay']
    game_path = paths['game_path']
    work_dir = paths['working_dir'] / "main"
    wcc_lite = paths['wcc_lite']

    output_dir = work_dir / "2_mod"
    output_mod_name = "modSmoothUIRemastered"
    output_content_path = output_dir / output_mod_name / "content"

    # Path to the output .zip for distribution.
    zip_out = resolve_zip_path(args.zip, f"{output_mod_name}.zip")

    if not uncooked_gameplay.is_dir():
        fail(f"not a directory: {uncooked_gameplay}")
    exclusions = load_exclusions()
    to_patch, excluded = select_files(uncooked_gameplay, exclusions)

    patched_dir = work_dir / "1_patched"
    if not args.dry_run:
        # Clean out stale output
        print_step_header(f"Cleaning {patched_dir}")
        shutil.rmtree(patched_dir, ignore_errors=True)
        output_content_path.mkdir(parents=True, exist_ok=True)

    print_step_header("Running FPS patcher")
    try:
        patched, kept, failed = patcher.patch_files(
            uncooked_gameplay, patched_dir / "gameplay", to_patch, settings,
            dry_run=args.dry_run, on_file=print_file_result)
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done: {len(patched)} patched, {len(kept)} left alone, "
          f"{len(excluded)} excluded, {len(failed)} skipped.")

    if args.dry_run:
        print_step_header("DRY RUN: exiting")
        return

    if not patched:
        fail("no patched files were produced, aborting.")

    print_step_header("Removing old bundle/metadata from output folder")
    for old in [*output_content_path.glob("blob*.bundle"), output_content_path / "metadata.store"]:
        old.unlink(missing_ok=True)

    # wcc_lite is picky about its working directory, and needs Windows-style paths even under WSL
    # ALL CAPS are used for Windows-style paths, lowercase for Python name (Windows or WSL)
    PATCHED_DIR = to_windows(patched_dir)
    CONTENT_PATH = to_windows(output_content_path)

    print_step_header("Running wcc_lite pack")
    run_command([wcc_lite, "pack", f"-dir={PATCHED_DIR}", f"-outdir={CONTENT_PATH}"], cwd=wcc_lite.parent)

    print_step_header("Running wcc_lite metadatastore")
    run_command([wcc_lite, "metadatastore", "-noui", f"-path={CONTENT_PATH}"], cwd=wcc_lite.parent)

    if not (output_content_path / "metadata.store").is_file() or not any(output_content_path.glob("blob*.bundle")):
        fail("wcc_lite did not produce blob*.bundle and metadata.store.")

    zip_tree(output_dir, zip_out)

    if args.install:
        install_mod(output_dir / output_mod_name, game_path)

    if args.keep_work:
        print(f"Keeping work files in {work_dir}")
    else:
        print_step_header(f"Removing {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)