#!/usr/bin/env python3

# build-compat.py -- take an existing mod .zip/.rar/.7z, apply framerate patches to the .redswf files
# that conflict with Smooth UI Remastered, and build a compatibility patch containing only
# those patched files.
#
# The files to patch and their target frame rates come from the profile file (see config/).
# Example usage: python build-compat.py modOtherUI.zip --mod-name OtherMod --mod-version 1.0 --smooth-version 0.2

import argparse
import shutil
import zipfile
from pathlib import Path

import py7zr
import rarfile

from . import patcher
from .build_common import (
    config_paths,
    fail,
    install_mod,
    launch_game,
    load_config,
    load_manifest,
    load_profile,
    print_file_result,
    print_step_header,
    resolve_zip_path,
    wcc_lite,
    zip_tree,
)
from .common import normalize_version, to_windows

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
                    help="version the other mod; only used to name the generated patch file")
    ap.add_argument("--smooth-version", required=True,
                    help="Smooth UI Remastered version that the patch targets; affects the patching process")
    ap.add_argument("--profile", default=None,
                    help="the profile to use; a profile specifies which files to patch, and to which framerate")

    ap.add_argument("--zip", default=None,
                    help="output .zip name or path (default: <generated mod name>.zip "
                         "in the repo folder)")
    ap.add_argument("--install", action="store_true",
                    help="install the new mod into the game folder on success")
    ap.add_argument("--launch", action="store_true",
                    help="launch the game after build and install success")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")
    return ap.parse_args(argv)

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
    """Extract the whole mod archive (.zip, .7z or .rar) into dest_dir."""
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

def find_unbundle_root(unpacked_dir):
    """The directory to give to wcc_lite unbundle: the modXxx folder if there is one,
    otherwise the folder that holds the bundle(s)."""
    content_dirs = sorted({b.parent for b in unpacked_dir.rglob("*.bundle")})
    if not content_dirs:
        fail("no .bundle files found in the .zip (mods shipping loose files are not supported)")
    if len(content_dirs) > 1:
        listing = "\n".join(f"  {d.relative_to(unpacked_dir)}" for d in content_dirs)
        fail(f"bundles found in several folders, this is not supported:\n{listing}")
    content = content_dirs[0]
    if content.name.lower() == "content" and content.parent.name.lower().startswith("mod"):
        return content.parent
    return content

def unbundle(root, out_dir):
    """Extract the bundle(s) under root into loose files under out_dir, at their depot paths"""
    out_dir.mkdir(parents=True, exist_ok=True)
    wcc_lite("unbundle", f"-dir={to_windows(root)}", f"-outdir={to_windows(out_dir)}")
    if not any(out_dir.rglob("*.redswf")):
        fail(f"no .redswf files found after unbundling into {out_dir}")

def find_shared_files(their_dir, manifest, smooth_version):
    """The .redswf files (paths relative to their_dir, in their spelling) that the other mod
    ships AND that the Smooth UI Remastered release named by manifest patches."""
    def key(rel):
        """One comparable spelling for both sides: lowercase, slash-separated"""
        return rel.as_posix().lower() if isinstance(rel, Path) else rel.lower()

    if manifest is None:
        fail(f"no manifest exists for Smooth UI Remastered {smooth_version}")

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

def main(argv=None):
    args = parse_args(argv)

    profile = load_profile(args.profile)

    if not args.mod_archive.is_file():
        fail(f"input mod archive not found: {args.mod_archive}")

    paths = config_paths(load_config())
    game_path = paths.get("game_path")

    if game_path is None and args.install:
        print("Warning: game_path not set in config.toml; will ignore --install")
    if game_path is None and args.launch:
        print("Warning: game_path not set in config.toml; will ignore --launch")

    mod_name = mod_folder_name(args.mod_name)
    mod_version = normalize_version(args.mod_version)
    smooth_version = normalize_version(args.smooth_version)

    zip_out = resolve_zip_path(args.zip, f"SmoothUIRemastered_{smooth_version}_{args.mod_name}_{mod_version}_Compat.zip")

    work_dir = paths["working_dir"] / "compat"
    unpacked_dir = work_dir / "1_unpacked"
    unbundled_dir = work_dir / "2_unbundled"
    patched_dir = work_dir / "3_patched"
    mod_root = work_dir / "4_mod"
    mod_dir = mod_root / mod_name
    content_dir = mod_dir / "content"

    print(f"Mod folder: {mod_name}")
    print(f"Output ZIP: {zip_out.name}")

    print_step_header(f"Cleaning {work_dir}")
    shutil.rmtree(work_dir, ignore_errors=True)
    content_dir.mkdir(parents=True)

    print_step_header(f"Unpacking {args.mod_archive}")
    unpack(args.mod_archive, unpacked_dir)

    print_step_header("Unbundling the other mod")
    unbundle(find_unbundle_root(unpacked_dir), unbundled_dir)

    print_step_header(f"Loading manifest for Smooth UI Remastered {smooth_version}")
    manifest = load_manifest(smooth_version)

    print_step_header("Finding files in common")
    shared = find_shared_files(unbundled_dir, manifest, smooth_version)

    print_step_header("Running FPS patcher")
    shared_keys = {rel.as_posix().lower() for rel in shared}
    shared_profile = {rel: fps for rel, fps in profile.items() if rel in shared_keys}

    try:
        patched, kept, failed = patcher.patch_files(
            unbundled_dir, patched_dir, shared_profile, on_file=print_file_result)
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

    print_step_header("Running wcc_lite pack")
    wcc_lite("pack", f"-dir={to_windows(patched_dir)}", f"-outdir={to_windows(content_dir)}")

    print_step_header("Running wcc_lite metadatastore")
    wcc_lite("metadatastore", "-noui", f"-path={to_windows(content_dir)}")

    if not (content_dir / "metadata.store").is_file() or not any(content_dir.glob("blob*.bundle")):
        fail("wcc_lite did not produce blob*.bundle and metadata.store.")

    zip_tree(mod_root, zip_out)

    if args.install:
        install_mod(mod_dir, game_path)
        if args.launch:
            launch_game(game_path)
    elif args.launch:
        print("\nWarning:--launch used without --install: not launching game with old mod version")

    if args.keep_work:
        print(f"Keeping work files in {work_dir}")
    else:
        print_step_header(f"Removing {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)