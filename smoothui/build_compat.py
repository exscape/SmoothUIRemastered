#!/usr/bin/env python3

# build-compat.py -- take an existing mod .zip, apply framerate patches to the .redswf files
# that conflict with Smooth UI Remastered, and build a compatibility patch containing only
# those patched files.
#
# Use the same rate options (--multiplier, --max-fps, ...) as for the main mod build.
# Example usage: python build-compat.py modOtherUI.zip --name OtherMod --multiplier 2 --max-fps 60

import argparse
import shutil
import zipfile
from pathlib import Path

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

# Used to name the generated mod. The "mod000_" prefix sorts before the main mod and
# (nearly) any other mod, so the compatibility patch loads first and wins conflicts.
MAIN_MOD_NAME = "SmoothUIRemastered"

# select_files() returns paths relative to the uncooked gameplay dir, whereas files unbundled
# from a mod start with the depot path, which includes this folder
DEPOT_PREFIX = "gameplay"

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Build a Smooth UI compatibility patch for an existing mod .zip")

    ap.add_argument("mod_zip", type=Path,
                    help="the other mod's .zip (must contain blob*.bundle)")
    ap.add_argument("--name", required=True,
                    help="name of the other mod, used in the generated mod's folder name ")

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fps", type=float, help="set this exact frame rate")
    g.add_argument("--multiplier", type=float, help="multiply the original rate")

    ap.add_argument("--max-fps", type=float, default=120,
                    help="clamp the target rate to this (default 120)")
    ap.add_argument("--skip-at-or-above", type=float, default=60,
                    help="leave files already at/above this rate alone (default 60)")
    ap.add_argument("--zip", default=None,
                    help="output .zip name or path (default: <generated mod name>.zip "
                         "in the repo folder)")
    ap.add_argument("--install", action="store_true",
                    help="install the new mod into the game folder on success")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    return ap.parse_args(argv)

def mod_folder_name(name):
    return f"mod000_{MAIN_MOD_NAME}_{name}_Compatibility"

def unzip(mod_zip, dest_dir):
    """Extract the whole mod .zip into dest_dir"""
    try:
        with zipfile.ZipFile(mod_zip) as z:
            z.extractall(dest_dir)
    except zipfile.BadZipFile:
        fail(f"not a valid .zip file: {mod_zip} (.7z and .rar are not supported)")
    count = sum(1 for f in dest_dir.rglob("*") if f.is_file())
    print(f"Unpacked {count} files into {dest_dir}")

def find_unbundle_root(unzipped_dir):
    """The directory to give to wcc_lite unbundle: the modXxx folder if there is one,
    otherwise the folder that holds the bundle(s).
    Handles mods that ship as mods/modXxx/content, modXxx/content, or just content."""
    content_dirs = sorted({b.parent for b in unzipped_dir.rglob("*.bundle")})
    if not content_dirs:
        fail("no .bundle files found in the .zip (mods shipping loose files are not supported)")
    if len(content_dirs) > 1:
        listing = "\n".join(f"  {d.relative_to(unzipped_dir)}" for d in content_dirs)
        fail(f"bundles found in several folders, this is not supported:\n{listing}")
    content = content_dirs[0]
    if content.name.lower() == "content" and content.parent.name.lower().startswith("mod"):
        return content.parent
    return content

def unbundle(root, out_dir, wcc_lite):
    """Extract the bundle(s) under root into loose files under out_dir, at their depot paths"""
    out_dir.mkdir(parents=True, exist_ok=True)
    run_command([wcc_lite, "unbundle", f"-dir={to_windows(root)}",
                 f"-outdir={to_windows(out_dir)}"], cwd=wcc_lite.parent)
    if not any(out_dir.rglob("*.redswf")):
        fail(f"no .redswf files found after unbundling into {out_dir}")

def find_shared_files(their_dir, uncooked_gameplay, exclusions):
    """The .redswf files (paths relative to their_dir, in their spelling) that the other mod
    ships AND that Smooth UI Remastered patches."""
    if not uncooked_gameplay.is_dir():
        fail(f"not a directory: {uncooked_gameplay}")
    ours, excluded = select_files(uncooked_gameplay, exclusions)

    def key(rel):
        return rel.as_posix().lower()

    our_keys = {f"{DEPOT_PREFIX}/{key(p)}" for p in ours}
    excluded_keys = {f"{DEPOT_PREFIX}/{key(p)}" for p in excluded}
    theirs = sorted(f.relative_to(their_dir) for f in their_dir.rglob("*.redswf"))
    shared = [p for p in theirs if key(p) in our_keys]

    print(f"The other mod has {len(theirs)} .redswf files, {len(shared)} of which "
          f"{MAIN_MOD_NAME} also patches")
    for p in theirs:
        if key(p) not in our_keys:
            why = "on our exclusion list" if key(p) in excluded_keys else "not in our mod"
            print(f"  not patched: {p} ({why})")

    if not shared:
        sample = "\n".join(f"  {p}" for p in theirs[:3])
        fail("no .redswf files in common, nothing to patch. Their paths start like:\n"
             f"{sample}\nOurs should start with '{DEPOT_PREFIX}/'. If this looks correct, this mod does not need a compat patch!")
    return shared

def main(argv=None):
    args = parse_args(argv)

    try:
        settings = PatchSettings(fps=args.fps, multiplier=args.multiplier,
                                 max_fps=args.max_fps, skip_at_or_above=args.skip_at_or_above)
    except ValueError as e:
        fail(str(e))

    if not args.mod_zip.is_file():
        fail(f"input mod .zip not found: {args.mod_zip}")

    paths = config_paths(load_config())
    uncooked_gameplay = paths["uncooked_gameplay"]
    game_path = paths["game_path"]
    wcc_lite = paths["wcc_lite"]

    mod_name = mod_folder_name(args.name)
    zip_out = resolve_zip_path(args.zip, f"{mod_name}.zip")
    exclusions = load_exclusions()

    work_dir = paths["working_dir"] / "compat"
    unzipped_dir = work_dir / "1_unzipped"
    unbundled_dir = work_dir / "2_unbundled"
    patched_dir = work_dir / "3_patched"
    mod_root = work_dir / "4_mod"
    mod_dir = mod_root / mod_name
    content_dir = mod_dir / "content"

    print(f"Generated mod name: {mod_name}")

    # Clean out stale output from earlier runs
    print_step_header(f"Cleaning {work_dir}")
    shutil.rmtree(work_dir, ignore_errors=True)
    content_dir.mkdir(parents=True)

    print_step_header(f"Unzipping {args.mod_zip}")
    unzip(args.mod_zip, unzipped_dir)

    print_step_header("Unbundling the other mod")
    unbundle(find_unbundle_root(unzipped_dir), unbundled_dir, wcc_lite)

    print_step_header("Finding files in common")
    shared = find_shared_files(unbundled_dir, uncooked_gameplay, exclusions)

    print_step_header("Running FPS patcher")
    try:
        patched, kept, failed = patcher.patch_files(
            unbundled_dir, patched_dir, shared, settings, on_file=print_file_result)
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done: {len(patched)} patched, {len(kept)} left alone, {len(failed)} failed.")

    # A conflicting file missing from the patch would let our main mod's version
    # override the other mod's changes to that screen.
    if failed:
        fail(f"{len(failed)} conflicting file(s) could not be patched, see above")
    if not patched:
        fail("no patched files were produced, aborting.")

    # wcc_lite is picky about its working directory, and needs Windows-style paths
    # even under WSL
    print_step_header("Running wcc_lite pack")
    run_command([wcc_lite, "pack", f"-dir={to_windows(patched_dir)}",
                 f"-outdir={to_windows(content_dir)}"], cwd=wcc_lite.parent)

    print_step_header("Running wcc_lite metadatastore")
    run_command([wcc_lite, "metadatastore", "-noui", f"-path={to_windows(content_dir)}"],
                cwd=wcc_lite.parent)

    if not (content_dir / "metadata.store").is_file() or not any(content_dir.glob("blob*.bundle")):
        fail("wcc_lite did not produce blob*.bundle and metadata.store.")

    zip_tree(mod_root, zip_out)

    if args.install:
        install_mod(mod_dir, game_path)

    if args.keep_work:
        print(f"Keeping work files in {work_dir}")
    else:
        print_step_header(f"Removing {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)