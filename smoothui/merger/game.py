"""Where the game and its mods.settings are, the command that starts the game, and the merger's settings
(data\\settings.json), which the merger writes when it first runs: the launcher opens the merger while
there are none.

Both exes find the game here, so nothing is passed between them. For tests only, two environment variables
override where the game and mods.settings are (SMOOTHUI_GAME_PATH, SMOOTHUI_MODS_SETTINGS); the merger
inherits them from the launcher. Light: the launcher imports it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ..common import fail, to_native, to_windows
from ..files import make_dirs, read_file, write_file
from ..paths import DATA_DIR
from ..witcher3.user_files import mods_settings_path

GAME_PATH_ENV = "SMOOTHUI_GAME_PATH"
MODS_SETTINGS_ENV = "SMOOTHUI_MODS_SETTINGS"

SETTINGS_FILE = "settings.json"

GAME_EXE = Path("bin", "x64_dx12", "witcher3.exe")

# TODO: hardcoded for now. The merger is to find the game in the registry, have the
# user confirm it or choose another folder, and save it in its settings; both exes then read it from there.
DEFAULT_GAME_PATH = r"D:\Games\The Witcher 3 Remastered"


@dataclass(frozen=True, slots=True)
class Settings:
    game_path: Path


def load_settings(data_dir: Path = DATA_DIR) -> Settings | None:
    """The merger's settings; None if the merger has never run"""
    path = Path(data_dir) / SETTINGS_FILE
    data = read_file(path)
    if data is None:
        return None
    try:
        settings = json.loads(data.decode("utf-8"))
        return Settings(game_path=to_native(settings["game_path"]))
    except (ValueError, KeyError, TypeError) as e:
        fail(f"{path} can't be read: {e}")


def save_settings(settings: Settings, data_dir: Path = DATA_DIR):
    make_dirs(data_dir)
    text = json.dumps({"game_path": to_windows(settings.game_path)}, indent=1) + "\n"
    write_file(Path(data_dir) / SETTINGS_FILE, text.encode("utf-8"))


def find_game() -> Path:
    """Where the game is, for settings that don't exist yet (see DEFAULT_GAME_PATH)"""
    return to_native(DEFAULT_GAME_PATH)


def game_path(settings: Settings | None) -> Path:
    if os.environ.get(GAME_PATH_ENV):
        return to_native(os.environ[GAME_PATH_ENV])
    return settings.game_path if settings else find_game()


def mods_settings() -> Path:
    if os.environ.get(MODS_SETTINGS_ENV):
        return to_native(os.environ[MODS_SETTINGS_ENV])
    return mods_settings_path()


class GameCommand(NamedTuple):
    args: list[str]
    cwd: Path | None        # where it starts; None: where the launcher was started
    note: str | None        # for the log, if it looks wrong


def game_command(args: list[str], game: Path) -> GameCommand:
    """The command that starts the game, from the launcher's arguments. A first argument ending in .exe
    makes them all the command (Steam passes its own, %command%), started where the launcher was;
    otherwise they are the game exe's arguments, and it starts in the exe's folder, as the game's own
    launchers do."""
    if args and args[0].lower().endswith(".exe"):
        exe = Path(os.path.abspath(args[0]))
        inside = os.path.normcase(exe).startswith(os.path.normcase(os.path.join(os.path.abspath(game), "")))
        return GameCommand(list(args), None, None if inside else f"the given exe isn't in the game folder: {exe}")
    exe = Path(game) / GAME_EXE
    return GameCommand([str(exe), *args], exe.parent, None)
