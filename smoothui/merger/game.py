"""Where the game and its mods.settings are, the command that starts the game, and the merger's settings
(data\\settings.json): the game folder the user confirmed in the merger, whether it checks for updates,
and when it last did, whether it merges the HUD, the frame rates to build for, and the screens the user
chose not to merge. The launcher
opens the merger while there are none.

The merger finds candidates for the game folder (find_games) for the user to confirm, and both exes read
it from the settings, so nothing is passed between them. For tests only, two environment variables
override where the game and mods.settings are (SMOOTHUI_GAME_PATH, SMOOTHUI_MODS_SETTINGS); the merger
inherits them from the launcher. Light: the launcher imports it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ..common import SmoothUIError, fail, to_native, to_windows
from ..files import make_dirs, read_file, write_file
from ..paths import APP_DIR, DATA_DIR
from ..windows import module_loaded
from ..witcher3.user_files import FrameSettings, mods_settings_path, read_frame_settings
from .scan import ScreenOff

GAME_PATH_ENV = "SMOOTHUI_GAME_PATH"
MODS_SETTINGS_ENV = "SMOOTHUI_MODS_SETTINGS"

SETTINGS_FILE = "settings.json"
# mods.settings as it was before the merger last changed it, in the merger's data folder
MODS_SETTINGS_BACKUP = "mods.settings.bak"

GAME_EXE = Path("bin", "x64_dx12", "witcher3.exe")
# Mod Organizer 2's virtual file system, loaded into the programs it starts
MO2_VFS_DLL = "usvfs_x64.dll"
# The frame rates to build for (see Settings.target_fps): (lowest, highest). The HUD's goes lower, for frame
# generation (the HUD advances once per rendered frame, half the frames shown with 2x). Below 48 (twice the
# game's 24) there's too little to gain, so a lower rate in gameplay gets a 48 fps HUD that plays slowly.
FPS_RANGES = {"target_fps": (60, 240), "hud_fps": (48, 240)}
# The lowest frame rate in gameplay (rendered, after frame generation) hud_rate builds for: below it, a HUD
# built for FPS_RANGES' lowest plays too slowly to be better than the game's own
HUD_LOWEST = 35
# The frame generation multipliers (see Settings.hud_fg): 1 is off
FG_MULTIPLIERS = range(1, 7)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settings:
    game_path: Path
    check_updates: bool = True      # whether the merger looks for updates when it starts (see updates.py)
    last_update_check: int | None = None    # when it last did (Unix time), if ever
    smooth_hud: bool = True         # whether the merger merges the HUD (see groups_off)
    # The mods' copies of screens the user chose not to merge (see scan.Scan.screens_off), oldest first. A
    # choice stays while the mod doesn't ship the screen, or another mod's copy wins: it applies again if
    # the mod's copy wins again.
    screens_off: tuple[ScreenOff, ...] = ()
    # The menus' frame rate to build for (see scan.Scan.target_fps); None: the installed Smooth UI build's
    target_fps: int | None = None
    # The frame rate in gameplay (what a counter shows) and the frame generation multiplier, which give the
    # HUD's rate (see hud_fps); None: the HUD at the installed build's rate
    hud_game_fps: int | None = None
    hud_fg: int = 1
    pending_update: str | None = None       # the newer version the last check found, if any (see updates.check)

    @property
    def hud_fps(self) -> int | None:
        return hud_rate(self.hud_game_fps, self.hud_fg)[0] if self.hud_game_fps is not None else None


def hud_rate(game_fps: int, fg: int) -> tuple[int, float, str]:
    """The HUD's frame rate to build for a frame rate in gameplay and a frame generation multiplier (the HUD
    advances once per rendered frame; at least FPS_RANGES' lowest), the speed it plays at (1.0, or less below
    that lowest: 40 fps plays a 48 fps HUD at 0.83), and how well it works: "ok"; "slowed" (below that lowest,
    from HUD_LOWEST: it plays somewhat slowly); "too-low" (below HUD_LOWEST) and "too-high" (above FPS_RANGES):
    refused"""
    rendered = round(game_fps / fg)
    lowest, highest = FPS_RANGES["hud_fps"]
    if rendered > highest:
        return rendered, 1.0, "too-high"
    rate = max(rendered, lowest)
    if rendered < HUD_LOWEST:
        return rate, rendered / rate, "too-low"
    return rate, rendered / rate, "ok" if rendered >= lowest else "slowed"


def default_game_fps(menus: int, fg: int, cap: int | None) -> int:
    """The rate in gameplay to suggest on the first run: 96 (a safe guess: a HUD built too high plays slowly, one
    built a little low still looks smooth), or with more frame generation what gives the HUD's lowest rate, 48
    rendered; at most the menus' rate and the game's limit (cap, None: none)."""
    return min(menus, max(96, FPS_RANGES["hud_fps"][0] * fg), cap or menus)


def groups_off(settings: Settings | None) -> frozenset[str]:
    """The groups of files (see build/profile.py's Group) the merger leaves out, as the settings say (None:
    their defaults): the HUD, unless Smooth HUD is on"""
    return frozenset() if settings is None or settings.smooth_hud else frozenset({"hud"})


def screens_off(settings: Settings | None) -> tuple[ScreenOff, ...]:
    """The screens the user chose not to merge (none for None)"""
    return settings.screens_off if settings else ()


def scan_options(settings: Settings | None) -> dict:
    """scan.scan's options from the settings (None: their defaults): what they turn off (groups_off,
    screens_off) and the frame rates. The scan also reads the reviews in use, whose dont-merge lines turn off
    more."""
    return {"groups_off": groups_off(settings), "screens_off": screens_off(settings),
            "target_fps": settings.target_fps if settings else None, "hud_fps": settings.hud_fps if settings else None}


def load_settings(data_dir: Path = DATA_DIR) -> Settings | None:
    """The merger's settings; None if the merger has never run. A setting missing from the file (it was
    saved by an older version) has its default."""
    path = Path(data_dir) / SETTINGS_FILE
    data = read_file(path)
    if data is None:
        return None
    try:
        settings = json.loads(data.decode("utf-8"))
        check_updates = settings.get("check_updates", True)
        if not isinstance(check_updates, bool):
            raise TypeError(f"check_updates is {check_updates!r}, not true or false")
        last_check = settings.get("last_update_check")
        if last_check is not None and type(last_check) is not int:
            raise TypeError(f"last_update_check is {last_check!r}, not a whole number")
        pending = settings.get("pending_update")
        if pending is not None and not isinstance(pending, str):
            raise TypeError(f"pending_update is {pending!r}, not text")
        smooth_hud = settings.get("smooth_hud", True)
        if not isinstance(smooth_hud, bool):
            raise TypeError(f"smooth_hud is {smooth_hud!r}, not true or false")
        off = settings.get("not_merged", [])
        if not isinstance(off, list):
            raise TypeError(f"not_merged is {off!r}, not a list")
        off = tuple(choice for choice in map(_screen_off, off) if choice is not None)
        target_fps, game_fps, fg = (settings.get(name) for name in ("target_fps", "hud_game_fps", "hud_fg"))
        lowest, highest = FPS_RANGES["target_fps"]
        if target_fps is not None and not (type(target_fps) is int and lowest <= target_fps <= highest):
            raise TypeError(f"target_fps is {target_fps!r}, not a whole number from {lowest} to {highest}")
        fg = 1 if fg is None else fg
        if not (type(fg) is int and fg in FG_MULTIPLIERS):
            raise TypeError(f"hud_fg is {fg!r}, not a whole number from 1 to {FG_MULTIPLIERS[-1]}")
        if game_fps is not None:
            if not (type(game_fps) is int and game_fps >= 1):
                raise TypeError(f"hud_game_fps is {game_fps!r}, not a whole number from 1")
            if hud_rate(game_fps, fg)[2] in ("too-low", "too-high"):
                raise TypeError(f"hud_game_fps {game_fps} with hud_fg {fg} can't be built for: the game renders "
                                f"{round(game_fps / fg)} fps, not from {HUD_LOWEST} to "
                                f"{FPS_RANGES['hud_fps'][1]}")
        return Settings(to_native(settings["game_path"]), check_updates, last_check, smooth_hud, off, target_fps,
                        game_fps, fg, pending_update=pending)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        fail(f"{path} can't be read: {e}")


def _screen_off(choice: dict) -> ScreenOff | None:
    """A not_merged entry of the settings file; what the choice was made with is left out if it can't be used
    (it only tells when merging may work now). None (logged) for an entry without its screen and mod: one
    bad entry mustn't make the whole file unreadable."""
    if not isinstance(choice, dict) or not isinstance(choice.get("screen"), str) or not isinstance(choice.get("mod"), str):
        log.warning(f"Skipped a screen not to merge that can't be read, in the merger's settings: {choice!r}")
        return None

    def number(name):
        value = choice.get(name)
        return value if type(value) is int else None

    def text(name):
        value = choice.get(name)
        return value if isinstance(value, str) else None

    return ScreenOff(choice["screen"], choice["mod"], text("since") or "", number("size"),
                     number("crc32"), text("reviews_sha256"), text("tools_version"))


def save_settings(settings: Settings, data_dir: Path = DATA_DIR):
    make_dirs(data_dir)
    fields = {"game_path": to_windows(settings.game_path), "check_updates": settings.check_updates,
              "smooth_hud": settings.smooth_hud}
    if settings.last_update_check is not None:
        fields["last_update_check"] = settings.last_update_check
    if settings.pending_update is not None:
        fields["pending_update"] = settings.pending_update
    if settings.target_fps is not None:
        fields["target_fps"] = settings.target_fps
    if settings.hud_game_fps is not None:
        fields |= {"hud_game_fps": settings.hud_game_fps, "hud_fg": settings.hud_fg}
    if settings.screens_off:
        fields["not_merged"] = [{"screen": choice.path, "mod": choice.mod, "since": choice.since,
                                 **({"size": choice.size, "crc32": choice.crc32} if choice.size is not None else {}),
                                 **({"reviews_sha256": choice.reviews} if choice.reviews else {}),
                                 **({"tools_version": choice.tools} if choice.tools else {})}
                                for choice in settings.screens_off]
    text = json.dumps(fields, indent=1) + "\n"
    write_file(Path(data_dir) / SETTINGS_FILE, text.encode("utf-8"))


def game_path(settings: Settings | None) -> Path | None:
    """The game folder: the test variable, else the settings; None while the user hasn't chosen one"""
    if os.environ.get(GAME_PATH_ENV):
        return to_native(os.environ[GAME_PATH_ENV])
    return settings.game_path if settings else None


def is_game_folder(folder: Path) -> bool:
    # The game's exe is a real file even under MO2, whose VFS only adds mods' files: is_file is fine here
    try:
        return (Path(folder) / GAME_EXE).is_file()
    except OSError:
        return False


def mo2_inside_game() -> bool:
    """Whether Mod Organizer 2 started this process (its virtual file system is loaded) from a folder inside
    a game folder: MO2 may then move the tools' own new files (settings, logs) to its overwrite folder or an
    output mod. Unsupported."""
    return module_loaded(MO2_VFS_DLL) and any(is_game_folder(folder) for folder in (APP_DIR, *APP_DIR.parents))


def game_folder_of(chosen: Path) -> Path:
    """The game folder, from a folder the user chose: the game folder itself, or a folder inside it.
    Fails (SmoothUIError) for any other folder."""
    chosen = Path(os.path.abspath(chosen))
    for folder in (chosen, *chosen.parents):
        if is_game_folder(folder):
            return folder
    fail(f"{to_windows(chosen)} isn't the game's folder: choose the folder that holds bin, content and Mods")


class GameFolder(NamedTuple):
    path: Path
    source: str | None      # where it was found, for the user: "GOG", "Steam", "Epic"; None: elsewhere


# Folders whose subfolders find_games looks into, on each fixed drive
COMMON_FOLDERS = (
    ("Games", None),
    ("GOG Games", "GOG"),
    (r"Program Files (x86)\GOG Galaxy\Games", "GOG"),
    (r"SteamLibrary\steamapps\common", "Steam"),
)

FIND_TIMEOUT = 1.0  # seconds


def find_games(timeout: float = FIND_TIMEOUT) -> list[GameFolder]:
    """The game folders that can be found quickly: from the registry (GOG, and the uninstall entries of
    any installer), Steam's libraries, Epic's manifests, then the folders one level inside COMMON_FOLDERS
    and those named like the game at the root of each fixed drive. Never a search of whole drives: what is
    found within timeout is returned, and the search goes on in the background (a sleeping hard disk or
    a slow drive can block it for seconds). Registry entries can be stale (an uninstalled game, a moved
    folder), so each folder is checked."""
    found = []
    search = threading.Thread(target=_search, args=(found,), daemon=True)
    search.start()
    search.join(timeout)
    games, seen = [], set()
    for game in found.copy():     # the search may still be adding to it
        key = os.path.normcase(os.path.normpath(game.path))
        if key not in seen:
            seen.add(key)
            games.append(game)
    return games


def _search(found: list[GameFolder]):
    """Appends each game folder found to found, the fastest sources first"""
    def add(folder, source):
        if folder and is_game_folder(folder):
            found.append(GameFolder(Path(folder), source))

    def add_inside(folder, source, name_filter=None):
        try:
            entries = [entry.path for entry in os.scandir(folder) if entry.is_dir()
                       and (name_filter is None or name_filter in entry.name.lower())]
        except OSError:
            return
        for path in entries:
            add(path, source)

    try:
        for folder, source in _registry_folders():
            add(folder, source)
        steam = _steam_path()
        for library in _steam_libraries(steam) if steam else []:
            add_inside(Path(library, "steamapps", "common"), "Steam")
        for folder in _epic_folders():
            add(folder, "Epic")
        for drive in _fixed_drives():
            for folder, source in COMMON_FOLDERS:
                add_inside(Path(drive, *folder.split("\\")), source)
            add_inside(drive, None, "witcher")
    except Exception:   # what was found until then still counts
        log.debug("Searching for the game failed", exc_info=True)


def _registry_folders() -> list[tuple[str, str | None]]:
    """(folder, source) from GOG's registry keys and the installers' uninstall entries for the game"""
    if sys.platform != "win32":
        return []
    import winreg

    folders = []
    for key, source in ((r"SOFTWARE\WOW6432Node\GOG.com\Games", "GOG"), (r"SOFTWARE\GOG.com\Games", "GOG")):
        for _, values in _subkeys(winreg.HKEY_LOCAL_MACHINE, key):
            folders.append((values.get("path"), source))
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for name, values in _subkeys(root, key):
                if "witcher 3" not in str(values.get("DisplayName", "")).lower():
                    continue
                # Steam's are "Steam App <id>"; GOG's installers (Inno Setup) "<GOG's id>_is1"
                source = "Steam" if name.startswith("Steam App ") else "GOG" if name.endswith("_is1") else None
                folders.append((values.get("InstallLocation"), source))
    return folders


def _subkeys(root, path) -> list[tuple[str, dict]]:
    """(name, {value name: value}) for each subkey of a registry key; none if it doesn't exist"""
    import winreg

    subkeys = []
    try:
        with winreg.OpenKey(root, path) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                name = winreg.EnumKey(key, i)
                try:
                    with winreg.OpenKey(key, name) as subkey:
                        values = {}
                        for j in range(winreg.QueryInfoKey(subkey)[1]):
                            value_name, value, _ = winreg.EnumValue(subkey, j)
                            values[value_name] = value
                except OSError:
                    continue
                subkeys.append((name, values))
    except OSError:
        pass
    return subkeys


def _steam_path() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return winreg.QueryValueEx(key, "SteamPath")[0]
    except OSError:
        return None


def _steam_libraries(steam: str) -> list[str]:
    """Steam's library folders: its own, and those steamapps\\libraryfolders.vdf names (Valve's KeyValues
    text: "path" "D:\\\\SteamLibrary" in each library's block; older files had "1" "D:\\\\SteamLibrary". Today's
    blocks also list "<app id>" "<size>", which aren't paths)"""
    libraries = [steam]
    try:
        text = Path(steam, "steamapps", "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries
    for match in re.finditer(r'^\s*"(path|\d+)"\s+"((?:[^"\\]|\\.)*)"\s*$', text, re.MULTILINE):
        path = match[2].replace("\\\\", "\\")
        if match[1] == "path" or re.match(r"[A-Za-z]:[\\/]|[\\/]", path):
            libraries.append(path)
    unique = {}     # Steam's own folder is usually listed again, spelled differently
    for path in libraries:
        unique.setdefault(os.path.normcase(os.path.normpath(path)), path)
    return list(unique.values())


def _epic_folders() -> list[str]:
    """The install folders in the Epic Games Launcher's manifests (one JSON .item file per game). Whether
    Epic sells this game is unverified: then there are none."""
    manifests = Path(os.environ.get("ProgramData", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "Data", "Manifests")
    folders = []
    try:
        paths = [entry.path for entry in os.scandir(manifests) if entry.name.lower().endswith(".item")]
    except OSError:
        return folders
    for path in paths:
        try:
            folders.append(json.loads(Path(path).read_bytes())["InstallLocation"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return folders


def _fixed_drives() -> list[str]:
    """The roots of the local, fixed drives (C:\\, D:\\, ...): no network, removable or optical drives"""
    if sys.platform != "win32":
        return []
    import ctypes

    drive_fixed = 3
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = [f"{chr(ord('A') + i)}:\\" for i in range(26) if mask & (1 << i)]
    return [root for root in roots if ctypes.windll.kernel32.GetDriveTypeW(root) == drive_fixed]


def mods_settings() -> Path:
    if os.environ.get(MODS_SETTINGS_ENV):
        return to_native(os.environ[MODS_SETTINGS_ENV])
    return mods_settings_path()


def frame_settings() -> FrameSettings | None:
    """The game's frame rate cap and frame generation, from the folder of mods.settings; None if unknown"""
    try:
        return read_frame_settings(mods_settings().parent)
    except SmoothUIError as e:
        log.warning(f"The game's frame rate settings can't be read: {e}")
        return None


class GameCommand(NamedTuple):
    args: list[str]
    cwd: Path | None        # where it starts; None: where the launcher was started
    note: str | None        # for the log, if it looks wrong


def game_command(args: list[str], game: Path | None) -> GameCommand | None:
    """The command that starts the game, from the launcher's arguments. A first argument ending in .exe
    makes them all the command (Steam passes its own, %command%), started where the launcher was;
    otherwise they are the game exe's arguments, and it starts in the exe's folder, as the game's own
    launchers do. None if no exe is given and the game folder isn't known."""
    if args and args[0].lower().endswith(".exe"):
        exe = Path(os.path.abspath(args[0]))
        if game is None:
            return GameCommand(list(args), None, None)
        inside = os.path.normcase(exe).startswith(os.path.normcase(os.path.join(os.path.abspath(game), "")))
        return GameCommand(list(args), None, None if inside else f"the given exe isn't in the game folder: {exe}")
    if game is None:
        return None
    exe = Path(game) / GAME_EXE
    return GameCommand([str(exe), *args], exe.parent, None)


# What the frozen merger's Qt (PySide6, and Nuitka's support for it) sets in its environment
QT_VARIABLES = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "PYSIDE6_OPTION_PYTHON_ENUM")


def game_environment(environ: dict[str, str], app_dir: Path | None) -> dict[str, str]:
    """The environment the merger starts the game with: its own (environ), without what loading Qt adds to it
    in the frozen merger (app_dir: the tools' folder; None when run from source): the Qt variables, and the
    tools' folder and its PySide6\\ at the front of PATH, where DLLs that the game or what it loads (overlays,
    ReShade) looks for could be found. The launcher doesn't load Qt: the game it starts has none of it."""
    env = dict(environ)
    if app_dir is None:
        return env
    for name in QT_VARIABLES:
        env.pop(name, None)
    # Resolved: PATH may have the folder's short name (C:\\PROGRA~1\\...)
    app = os.path.normcase(os.path.realpath(app_dir))
    def inside(folder):
        real = os.path.normcase(os.path.realpath(folder))
        return real == app or real.startswith(os.path.join(app, ""))
    for name, value in env.items():
        if name.upper() == "PATH":
            env[name] = os.pathsep.join(folder for folder in value.split(os.pathsep) if folder and not inside(folder))
    return env
