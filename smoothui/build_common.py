"""Helpers shared by the build scripts (build_main, build_compat)"""

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from colorama import Back, Fore, Style

from .common import to_native

REPO_ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_FILE = REPO_ROOT / "config" / "excluded-paths.txt"

CONFIG_KEYS = ("uncooked_gameplay", "game_path", "working_dir", "wcc_lite")

def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)

def print_step_header(msg):
    eq = "=" * 10
    print(f"{Back.BLACK}{Fore.YELLOW}{eq} {msg} {eq}{Style.RESET_ALL}", flush=True)

def run_command(cmd, cwd=None):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    try:
        subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)
    except FileNotFoundError:
        fail(f"executable not found: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        fail(f"command failed with exit code {e.returncode}: {cmd[0]}")

def load_config(path=None, section=None):
    """Read config.toml (or the given file) and perform basic validation"""
    path = path or REPO_ROOT / "config" / "config.toml"
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        fail(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        fail(f"invalid config file {path}: {e}")
    if section is not None:
        if section not in config:
            fail(f"missing section {section} in {path}")
        return config[section]

    paths = config.get("paths", {})
    missing = [k for k in CONFIG_KEYS if k not in paths]
    if missing:
        fail(f"missing in [paths] of {path}: {', '.join(missing)}")
    return config

def config_paths(config):
    """The config's paths, converted to Path objects usable by this Python"""
    return {k: to_native(v) for k, v in config["paths"].items()}

def load_exclusions():
    """Read the exclusion list: one dir or file (relative to the input dir) per line"""
    if not EXCLUDE_FILE.is_file():
        fail(f"exclude file not found: {EXCLUDE_FILE}")

    entries = []
    for line in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        e = line.replace("\\", "/").lower()
        e = e.strip("/")
        if e:
            entries.append(e)
    print(f"Loaded {len(entries)} exclusion entries from {EXCLUDE_FILE}")
    return entries

def match_exclusion(rel_posix_lower, entries):
    """Return the matching entry (file path or directory prefix), or None"""
    for e in entries:
        if rel_posix_lower == e or rel_posix_lower.startswith(e + "/"):
            return e
    return None

def select_files(src, exclusions):
    """Find the .redswf files under src that are not excluded.
    Returns (relative paths to patch, number excluded)"""
    selected, excluded = [], []
    for f in sorted(src.rglob("*.redswf")):
        rel_path = f.relative_to(src)
        if match_exclusion(rel_path.as_posix().lower(), exclusions):
            excluded.append(rel_path)
        else:
            selected.append(rel_path)
    return selected, excluded

def print_file_result(rel, status, old_fps, new_fps, note):
    """on_file callback for patcher.patch_files"""
    if status == "patched":
        print(f"{rel}: {old_fps:g} -> {new_fps:g} fps ({note})", flush=True)
    elif status == "kept":
        print(f"KEEP {rel}: {old_fps:g} fps (not changed)", flush=True)
    else:
        print(f"SKIP {rel}: {note}", file=sys.stderr, flush=True)

def resolve_zip_path(zip_arg, default_name):
    """Output .zip for a build: bare name goes in the repo folder, .zip suffix added"""
    zip_arg = zip_arg or default_name
    if "/" not in zip_arg and "\\" not in zip_arg:
        zip_out = REPO_ROOT / zip_arg
    else:
        zip_out = Path(zip_arg)
    if zip_out.suffix.lower() != ".zip":
        zip_out = zip_out.with_name(zip_out.name + ".zip")
    return zip_out

def zip_tree(src_dir, zip_out):
    """Zip every file under src_dir, keeping the tree's layout at the root of the archive"""
    print_step_header("Creating output .zip")
    zip_out.parent.mkdir(parents=True, exist_ok=True)
    zip_out.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src_dir))
    print(f"Created {zip_out} ({zip_out.stat().st_size:,} bytes)")

def install_mod(mod_dir, game_path):
    """Copy <mod_dir> (a directory whose name is the mod's name) into <game_path>/mods"""
    print_step_header("Installing mod to game folder")
    if not game_path.is_dir():
        fail(f"game path not found: {game_path}")
    dest = game_path / "mods" / Path(mod_dir).name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(mod_dir, dest)
    print(f"Installed mod into {dest}")
