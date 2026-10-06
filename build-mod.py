#!/usr/bin/env python3

# build-mod.py -- patch UI frame rates, then pack the mod with wcc_lite.
# All arguments except --zip are forwarded to fps-patcher.py.
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

from colorama import Back, Fore, Style, just_fix_windows_console

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

def nativize_path(p):
    """Ensure path is usable by this Python (converts D:\\... to /mnt/d/... in WSL)."""
    IN_WSL = (platform.system() == "Linux"
            and "microsoft" in platform.uname().release.lower())
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


def main():
    script_dir = Path(__file__).resolve().parent
    patcher = script_dir / "fps-patcher.py"

    mod_content = nativize_path(MOD_CONTENT)
    uncooked_gameplay = nativize_path(UNCOOKED_GAMEPLAY)
    build_dir = nativize_path(BUILD_DIR)
    wcc_dir = nativize_path(WCC_DIR)
    mod_dir = mod_content.parent
    mod_name = mod_dir.name

    # Consume build script options; forward everything else to the patcher
    ap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    ap.add_argument("--zip", default=None)
    args, patcher_args = ap.parse_known_args()
    dry_run = "--dry-run" in patcher_args

    # Path to the output .zip for distribution.
    # Place the output file in the script folder unless specified;
    # add .zip if a name without is specified
    zip_arg = args.zip or f"{mod_name}.zip"
    if "/" not in zip_arg and "\\" not in zip_arg:
        zip_out = script_dir / zip_arg
    else:
        zip_out = Path(zip_arg)
    if zip_out.suffix.lower() != ".zip":
        zip_out = zip_out.with_name(zip_out.name + ".zip")

    # Clean out stale output
    step(f"Cleaning {build_dir / 'gameplay'}")
    shutil.rmtree(build_dir / "gameplay", ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    mod_content.mkdir(parents=True, exist_ok=True)

    step("Running FPS patcher")
    run([sys.executable, patcher, *patcher_args,
         uncooked_gameplay, build_dir / "gameplay"])

    if dry_run:
        step("DRY RUN: exiting")
        return

    if not any((build_dir / "gameplay").rglob("*.redswf")):
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

if __name__ == "__main__":
    just_fix_windows_console() # Initialize colorama
    main()
