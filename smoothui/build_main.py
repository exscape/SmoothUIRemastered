#!/usr/bin/env python3

# build-mod.py -- patch UI frame rates, then pack the mod with wcc_lite.
# Example usage: python build-mod.py --multiplier 2 --max-fps 60 --skip-at-or-above 48
#
# Tested on Windows 11: via WSL and via cmd.exe (mostly via WSL)

import argparse
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from colorama import Back, Fore, Style

from . import patcher
from .patcher import PatchSettings

# ---- fixed paths (modders: edit this section to use your paths) ---------------------------
# Write these as Windows paths; if you use WSL (not required!) they are converted automatically.

# Path to the uncooked gameplay files.
# Generate by downloading REDkit and using wcc_lite uncook.
UNCOOKED_GAMEPLAY = r"E:\Temp\Witcher3Modding\RemasteredUncooked5.00c\gameplay"

# Path to the game directory where the mod is placed
MOD_CONTENT = r"D:\Games\The Witcher 3 Remastered\mods\modSmoothUIRemastered\content"

# Where the generated files are stored until the mod is packaged for distribution
BUILD_DIR = r"E:\Temp\Witcher3Modding\SmoothUIRemaster_Build"

# Path to wcc_lite.exe
WCC_DIR = r"D:\Games\The Witcher 3 Remastered\The Witcher 3 REDkit\bin\x64_RedKit"

# ---- the rest of the script is intended to work without edits -----------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXCLUDE_FILE = REPO_ROOT / "config" / "excluded-paths.txt"

IN_WSL = (platform.system() == "Linux"
          and "microsoft" in platform.uname().release.lower())

def nativize_path(p):
    """Ensure path is usable by this Python (converts D:\\... to /mnt/d/... in WSL)."""
    if IN_WSL and re.match(r"^[A-Za-z]:[\\/]", p):
        p = subprocess.check_output(["wslpath", "-u", p], text=True).strip()
    return Path(p)

def step(msg):
    eq = "=" * 10
    print(f"{Back.BLACK}{Fore.YELLOW}{eq} {msg} {eq}{Style.RESET_ALL}", flush=True)

def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)

def run(cmd, cwd=None):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    try:
        subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)
    except FileNotFoundError:
        fail(f"executable not found: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        fail(f"command failed with exit code {e.returncode}: {cmd[0]}")

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Patch vanilla UI frame rates, then create a release-ready mod .zip")

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fps", type=float, help="set this exact frame rate")
    g.add_argument("--multiplier", type=float, help="multiply the original rate")

    ap.add_argument("--max-fps", type=float, default=120,
                    help="clamp the target rate to this (default 120)")
    ap.add_argument("--skip-at-or-above", type=float, default=60,
                    help="leave files already at/above this rate alone (default 60)")
    ap.add_argument("--exclude-file", type=Path, default=None,
                    help="list of dirs/files to skip "
                         f"(default: {DEFAULT_EXCLUDE_FILE.relative_to(REPO_ROOT)}, if present)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be patched, without writing or packing anything")
    ap.add_argument("--zip", default=None,
                    help="output .zip name or path (default: <mod name>.zip in the repo folder)")
    return ap.parse_args(argv)

def load_exclusions(path):
    """Read the exclusion list: one dir or file (relative to the input dir) per line."""
    if path is None:
        path = DEFAULT_EXCLUDE_FILE
        if not path.is_file():
            return []
    elif not path.is_file():
        fail(f"exclude file not found: {path}")

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        e = line.replace("\\", "/").lower()
        e = e.strip("/")
        if e:
            entries.append(e)
    print(f"Loaded {len(entries)} exclusion entries from {path}")
    return entries

def match_exclusion(rel_posix_lower, entries):
    """Return the matching entry (file path or directory prefix), or None."""
    for e in entries:
        if rel_posix_lower == e or rel_posix_lower.startswith(e + "/"):
            return e
    return None

def select_files(src, exclusions):
    """Find the .redswf files under src that are not excluded.
    Returns (relative paths to patch, number excluded)."""
    selected, excluded, used = [], 0, set()
    for f in sorted(src.rglob("*.redswf")):
        rel = f.relative_to(src)
        hit = match_exclusion(rel.as_posix().lower(), exclusions)
        if hit:
            used.add(hit)
            excluded += 1
        else:
            selected.append(rel)
    return selected, excluded

def print_file_result(rel, status, old_fps, new_fps, note):
    if status == "patched":
        print(f"{rel}: {old_fps:g} -> {new_fps:g} fps ({note})", flush=True)
    elif status == "kept":
        print(f"KEEP {rel}: {old_fps:g} fps (not changed)", flush=True)
    else:
        print(f"SKIP {rel}: {note}", file=sys.stderr, flush=True)

def main(argv=None):
    args = parse_args(argv)

    try:
        settings = PatchSettings(fps=args.fps, multiplier=args.multiplier,
                                 max_fps=args.max_fps, skip_at_or_above=args.skip_at_or_above)
    except ValueError as e:
        fail(str(e))

    mod_content = nativize_path(MOD_CONTENT)
    uncooked_gameplay = nativize_path(UNCOOKED_GAMEPLAY)
    build_dir = nativize_path(BUILD_DIR)
    wcc_dir = nativize_path(WCC_DIR)
    mod_dir = mod_content.parent
    mod_name = mod_dir.name

    # Path to the output .zip for distribution.
    # Place the output file in the repo folder unless specified;
    # add .zip if a name without is specified
    zip_arg = args.zip or f"{mod_name}.zip"
    if "/" not in zip_arg and "\\" not in zip_arg:
        zip_out = REPO_ROOT / zip_arg
    else:
        zip_out = Path(zip_arg)
    if zip_out.suffix.lower() != ".zip":
        zip_out = zip_out.with_name(zip_out.name + ".zip")

    if not uncooked_gameplay.is_dir():
        fail(f"not a directory: {uncooked_gameplay}")
    exclusions = load_exclusions(args.exclude_file)
    to_patch, excluded_count = select_files(uncooked_gameplay, exclusions)

    patched_dir = build_dir / "gameplay"
    if not args.dry_run:
        # Clean out stale output
        step(f"Cleaning {patched_dir}")
        shutil.rmtree(patched_dir, ignore_errors=True)
        build_dir.mkdir(parents=True, exist_ok=True)
        mod_content.mkdir(parents=True, exist_ok=True)

    step("Running FPS patcher")
    try:
        patched, kept, failed = patcher.patch_files(
            uncooked_gameplay, patched_dir, to_patch, settings,
            dry_run=args.dry_run, on_file=print_file_result)
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done: {len(patched)} patched, {len(kept)} left alone, "
          f"{excluded_count} excluded, {len(failed)} skipped.")

    if args.dry_run:
        step("DRY RUN: exiting")
        return

    if not patched:
        fail("no patched files were produced, aborting.")

    step("Removing old bundle/metadata from mod folder")
    for old in [*mod_content.glob("blob*.bundle"), mod_content / "metadata.store"]:
        old.unlink(missing_ok=True)

    # wcc_lite is picky about its working directory, and needs Windows-style paths even under WSL
    wcc = wcc_dir / "wcc_lite.exe"
    step("Running wcc_lite pack")
    run([wcc, "pack", f"-dir={BUILD_DIR}", f"-outdir={MOD_CONTENT}"], cwd=wcc_dir)

    step("Running wcc_lite metadatastore")
    run([wcc, "metadatastore", "-noui", f"-path={MOD_CONTENT}"], cwd=wcc_dir)

    if not (mod_content / "metadata.store").is_file() or not any(mod_content.glob("blob*.bundle")):
        fail("wcc_lite did not produce blob*.bundle and metadata.store.")

    step("Creating output .zip")
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    zip_out.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(mod_dir.rglob("*")):
            if f.is_file():
                z.write(f, Path(mod_name) / f.relative_to(mod_dir))

    print(f"Added files to {zip_out} ({zip_out.stat().st_size:,} bytes)")