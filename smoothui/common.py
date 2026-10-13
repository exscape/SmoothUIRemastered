import platform
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

from colorama import Back, Fore, Style


def fail(msg):
    print(f"{Style.BRIGHT}{Fore.RED} ERROR: {msg}{Style.RESET_ALL}", file=sys.stderr, flush=True)
    sys.exit(1)

def print_step_header(msg):
    _print_header(msg, f"{Back.BLACK}{Fore.GREEN}", "=", 130)

def print_substep_header(msg):
    _print_header(msg, f"{Fore.CYAN}", "-", 90, align_start=True)

def _print_header(msg, color, symbol, width, align_start=False):
    if align_start:
        NUM_SYMBOLS = max(width - 1 - len(msg), 5)
        symbols = symbol * NUM_SYMBOLS
        text = f"{msg} {symbols}"
    else:
        NUM_SYMBOLS = max(5, (width - len(msg) - 2)// 2)
        symbols = symbol * NUM_SYMBOLS
        text = f"{symbols} {msg} {symbols}"

    if len(text) % 2 != 0: text += symbol
    print(f"\n{color}{text}{Style.RESET_ALL}", flush=True)

WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

IN_WSL = (platform.system() == "Linux"
          and "microsoft" in platform.uname().release.lower())

@cache
def _wslpath(flag, path):
    return subprocess.check_output(["wslpath", flag, path], text=True).strip()

def to_native(path):
    """Config-style (Windows) path -> Path usable by this Python.
    Converts D:\\... to /mnt/d/... in WSL; anything else passes through unchanged"""
    path = str(path)
    if IN_WSL and WIN_DRIVE_RE.match(path):
        path = _wslpath("-u", path)
    return Path(path)

def to_windows(path):
    """Native Path (or str) -> Windows-style string, e.g. for passing to wcc_lite"""
    path = str(path)
    if IN_WSL and not WIN_DRIVE_RE.match(path):
        return _wslpath("-w", path)
    return str(Path(path)) if not IN_WSL else path.replace("/", "\\")

def normalize_version(ver):
    """Normalize version strings: ensure they start with 'v', e.g. 0.1 => v0.1, v0.1 => v0.1"""
    ver = str(ver).strip()
    if not ver.startswith("v"):
        ver = "v" + ver
    return ver

def profile_key(rel):
    """One comparable spelling for a profile path: lowercase, slash-separated"""
    return str(rel).replace("\\", "/").lower().strip("/")
