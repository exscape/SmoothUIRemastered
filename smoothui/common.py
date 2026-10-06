import platform
import re
import subprocess
from functools import cache
from pathlib import Path

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