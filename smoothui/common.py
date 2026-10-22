import logging
import platform
import re
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import NoReturn

log = logging.getLogger(__name__)

class SmoothUIError(Exception):
    """An expected failure with a message for the user (bad input, config or environment), raised by fail()."""

def fail(msg) -> NoReturn:
    raise SmoothUIError(msg)

def run_cli(main):
    """Run a command-line entry point with console output (see log.py): log the message of a SmoothUIError
    (see fail()) and exit with code 1, without a traceback. Any other exception keeps its traceback."""
    # Imported here, not at the top: colorama is for the command line only
    from .log import setup_console
    setup_console()
    try:
        main()
    except SmoothUIError as e:
        log.error(str(e))
        sys.exit(1)

WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# sys.platform first: on Windows, platform.system() takes tens of milliseconds
IN_WSL = (sys.platform == "linux" and platform.system() == "Linux"
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
