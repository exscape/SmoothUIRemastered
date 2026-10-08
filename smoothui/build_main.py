#!/usr/bin/env python3

# build-mod.py -- patch UI frame rates, then pack the mod with wcc_lite.
# The files to patch and their target frame rates come from the profile file (see config/).
#
# Tested on Windows 11: via WSL and via cmd.exe (mostly via WSL)

import argparse
import datetime
import shutil

from . import patcher
from .build_common import (
    REPO_ROOT,
    config_paths,
    fail,
    install_mod,
    launch_game,
    load_config,
    load_profile,
    manifest_path,
    print_file_result,
    print_step_header,
    resolve_zip_path,
    sha256_file,
    wcc_lite,
    write_manifest,
    zip_tree,
)
from .common import to_windows

OUTPUT_MOD_NAME = "modSmoothUIRemastered"

def parse_args():
    ap = argparse.ArgumentParser(
        description="Patch vanilla UI frame rates, then create a release-ready mod .zip")

    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be patched, without writing or packing anything")
    ap.add_argument("--zip", default=None,
                    help="override output .zip name or path")
    ap.add_argument("--profile", default=None,
                    help="the profile to use; a profile specifies which files to patch, and to which framerate")
    ap.add_argument("--install", action="store_true",
                    help="install the mod into the game folder on success")
    ap.add_argument("--launch", action="store_true",
                    help="launch the game after build and install success")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    ap.add_argument("--write-manifest", action="store_true",
                    help="save a manifest of this version; only intended for actual releases")
    ap.add_argument("--force", action="store_true",
                    help="create a release version even though this version already seems to exist")
    return ap.parse_args()

def main():
    args = parse_args()

    config = load_config()
    paths = config_paths(config)
    profile = load_profile(args.profile)

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
    try:
        patched, kept, failed = patcher.patch_files(
            uncooked_files, patched_dir, profile,
            dry_run=args.dry_run, on_file=print_file_result)
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done: {len(patched)} patched, {len(kept)} left alone, "
          f"{len(failed)} skipped.")

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

    # wcc_lite is picky about its working directory, and needs Windows-style paths even under WSL
    # ALL CAPS are used for Windows-style paths, lowercase for Python name (Windows or WSL)
    PATCHED_DIR = to_windows(patched_dir)
    CONTENT_PATH = to_windows(output_content_path)

    print_step_header("Running wcc_lite pack")
    wcc_lite("pack", f"-dir={PATCHED_DIR}", f"-outdir={CONTENT_PATH}")

    print_step_header("Running wcc_lite metadatastore")
    wcc_lite("metadatastore", "-noui", f"-path={CONTENT_PATH}")

    if not (output_content_path / "metadata.store").is_file() or not any(output_content_path.glob("blob*.bundle")):
        fail("wcc_lite did not produce blob*.bundle and metadata.store.")

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
                "output_size": dst.stat().st_size, # source and output are always the same size, no point in stating both
                "fps": entry["fps"],
            }
        write_manifest(mod_version, records, extra={
            "targeted_game_version": config['versions'].get('targeted_game_version'),
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "output_zip": str(zip_out.relative_to(REPO_ROOT)) if zip_out.is_relative_to(REPO_ROOT) else str(zip_out),
        })

    if args.install:
        install_mod(output_dir / OUTPUT_MOD_NAME, game_path)

        if args.launch:
            launch_game(game_path)
    elif args.launch:
        print("\nWarning:--launch used without --install: not launching game with old mod version")

    if args.keep_work:
        print(f"Keeping work files in {work_dir}")
    else:
        print_step_header(f"Removing {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)