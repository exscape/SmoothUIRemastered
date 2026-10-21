"""Where the game keeps its user files: <Documents>\\The Witcher 3\\ (mods.settings, modioModsConfig.json, ...)."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

from ..common import IN_WSL, fail, to_native

CSIDL_PERSONAL = 5      # the Documents folder
MAX_PATH = 260

GAME_DIR_NAME = "The Witcher 3"
MODS_SETTINGS = "mods.settings"
MODIO_CONFIG = "modioModsConfig.json"


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
