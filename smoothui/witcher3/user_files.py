"""Where the game keeps its user files: <Documents>\\The Witcher 3\\ (mods.settings, modioModsConfig.json, ...),
and the frame rate settings in its dx12user.settings."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from ..common import IN_WSL, fail, to_native

CSIDL_PERSONAL = 5      # the Documents folder
MAX_PATH = 260

GAME_DIR_NAME = "The Witcher 3"
MODS_SETTINGS = "mods.settings"
MODIO_CONFIG = "modioModsConfig.json"
GAME_SETTINGS = "dx12user.settings"


class FrameSettings(NamedTuple):
    cap: int | None         # the frame rate limit (it limits the frames shown); None: unlimited
    fg_type: str | None     # the frame generation in use, as its name is shown: "DLSS", "FSR", "XeSS"; None: off
    fg: int                 # its multiplier; 1: off


def documents_folder() -> Path:
    if sys.platform == "win32":
        buffer = ctypes.create_unicode_buffer(MAX_PATH)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buffer)
        if result != 0 or not buffer.value:
            fail(f"Windows didn't say where the Documents folder is (SHGetFolderPathW: {result:#x})")
        return Path(buffer.value)
    if IN_WSL:
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                  "[Environment]::GetFolderPath('MyDocuments')"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as e:
            fail(f"can't ask Windows where the Documents folder is (powershell.exe): {e}")
        if not out:
            fail("Windows didn't say where the Documents folder is")
        return to_native(out)
    fail("the Documents folder can only be found on Windows (or WSL)")


def user_dir() -> Path:
    """<Documents>\\The Witcher 3"""
    return documents_folder() / GAME_DIR_NAME


def mods_settings_path() -> Path:
    return user_dir() / MODS_SETTINGS


def read_frame_settings(folder: Path) -> FrameSettings | None:
    """The frame rate settings in folder's dx12user.settings; None if it can't be read. A missing or odd value
    counts as off. The multipliers aren't confirmed for DLSS (frames generated + 1) and XeSS (2x)."""
    try:
        text = (folder / GAME_SETTINGS).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values = {}
    section = None
    for line in map(str.strip, text.splitlines()):
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line:
            key, value = line.split("=", 1)
            values[section, key.strip()] = value.strip()

    def number(section, key) -> int:
        try:
            return int(values.get((section, key), 0))
        except ValueError:
            return 0

    cap = number("Engine", "LimitFPS")
    fg_type, fg = None, 1
    if number("PostProcess", "DLSSGMode") > 0:
        fg_type, fg = "DLSS", max(number("PostProcess", "DLSSGNumFramesToGenerate"), 1) + 1
    elif number("PostProcess", "FSRFramegen") > 0:
        fg_type, fg = "FSR", 2
    elif values.get(("PostProcess", "XessFrameGeneration"), "").lower() == "true":
        fg_type, fg = "XeSS", 2
    return FrameSettings(cap if cap > 0 else None, fg_type, fg)
