"""The launcher (SmoothUILauncher.exe, see launcher.py): starts the game, and first opens the merger when the
package is stale (see merger/state.py: check) or the merger has never run.

Mod managers and Steam start it instead of the game, with the game's command or its arguments (see
merger/game.py: game_command), and track it as the game: so it waits for the game, and exits with its exit
code. It must never keep anyone from playing: when the check or the merger fails, it says so in a message
box, then starts the game anyway. Only a game that can't start ends it.

It writes nothing but its log (data\\launcher.log, rewritten on each start); failing to write that stops
nothing. Light: no Tk, and none of the patcher (see tests/test_state.py: LightImports).
"""

from __future__ import annotations

import subprocess
import sys
import time
import traceback
from pathlib import Path

from .common import SmoothUIError
from .files import make_dirs
from .merger import merger_version
from .merger.game import (
    GameCommand,
    game_command,
    game_path,
    load_settings,
    mods_settings,
)
from .merger.state import check
from .paths import APP_DIR, DATA_DIR, FROZEN
from .windows import MB_ICONERROR, ms_since_process_start, msg_box

LOG_FILE = "launcher.log"
MERGER_EXE = "SmoothUIMerger.exe"

# The merger's exit codes when the launcher opened it (--stale). 0 means its window was closed: the user
# wants to play. Anything else means it crashed (an unhandled exception, or ended in Task Manager, gives 1).
PLAY = 10
CLOSED = 0
ABORT = 11
UPDATING = 12       # it is updating itself: don't start the game

DETAILS = r"Details are in data\launcher.log."


class Log:
    """data\\launcher.log: each line with the time since the process was created"""
    def __init__(self, path: Path):
        self.start = time.perf_counter()
        self.offset = ms_since_process_start() or 0.0
        try:
            make_dirs(path.parent)
            self.file = open(path, "w", encoding="utf-8")   # noqa: SIM115 (open while the launcher runs)
        except (SmoothUIError, OSError):
            self.file = None

    def __call__(self, text: str):
        if self.file is None:
            return
        ms = self.offset + (time.perf_counter() - self.start) * 1000
        try:
            self.file.write(f"{ms:7.0f} ms  {text}\n")
            self.file.flush()
        except OSError:
            pass

    def failure(self, what: str, e: BaseException) -> str:
        """Log a failure with its traceback (unless it's a SmoothUIError, whose message says it all);
        returns its text for a message box"""
        text = str(e) if isinstance(e, SmoothUIError) else f"{type(e).__name__}: {e}"
        self(f"{what}: {text}")
        if not isinstance(e, SmoothUIError):
            self("".join(traceback.format_exception(e)).rstrip())
        return text


def main(args: list[str], log: Log) -> int:
    try:
        version = merger_version()
    except Exception as e:  # noqa: BLE001 (logged only)
        version = f"unknown ({e})"
    log(f"Smooth UI launcher {version}; arguments: {subprocess.list2cmdline(args) or '(none)'}")

    try:
        settings = load_settings()
    except Exception as e:  # noqa: BLE001 (the merger says so too, and writes them again)
        log.failure("The merger's settings can't be read", e)
        settings = None
    game = game_path(settings)
    command = game_command(args, game)
    log(f"Game folder: {game}")
    log(f"Game command: {subprocess.list2cmdline(command.args)}")
    if command.note:
        log(f"Note: {command.note}")

    if settings is None:
        log("The merger has never run: opening it")
        if not open_merger(log):
            return 0
    elif package_stale(game, log) and not open_merger(log):
        return 0
    return start_game(command, log)


def package_stale(game: Path, log: Log) -> bool:
    """Whether the check says to open the merger; False if it fails (after a message box)"""
    start = time.perf_counter()
    try:
        found = check(game, mods_settings())
    except Exception as e:  # noqa: BLE001 (the game starts anyway)
        text = log.failure("The check failed", e)
        msg_box(f"Smooth UI couldn't check whether its patches for your other mods are up to date:\n\n{text}\n\n"
                f"The game starts anyway. {DETAILS}")
        return False
    log(f"Checked in {(time.perf_counter() - start) * 1000:.0f} ms: {found.scan.installed.message}")
    if found.package_without_smoothui:
        log("A package is installed, without a usable Smooth UI")
    for state in found.states:
        if state.severity is not None:
            log(f"  {state.path}: {state.state.value} ({state.severity.name.lower()}): {state.message}")
    if not found.stale:
        log("Up to date")
        return False
    log(f"Stale (playing without merging: {found.severity.name.lower()}): opening the merger")
    return True


def open_merger(log: Log) -> bool:
    """Run the merger in stale mode and wait for it; returns whether to start the game"""
    if FROZEN:
        command = [str(APP_DIR / MERGER_EXE), "--stale"]
    else:
        command = [sys.executable, str(APP_DIR / "merger-gui.py"), "--stale"]
    try:
        code = subprocess.run(command, check=False).returncode
    except Exception as e:  # noqa: BLE001 (the game starts anyway)
        text = log.failure("The merger can't be started", e)
        msg_box(f"Smooth UI's merger can't be started:\n\n{text}\n\nThe game starts anyway. {DETAILS}")
        return True
    if code in (PLAY, CLOSED):
        log(f"The merger says: play{' (its window was closed)' if code == CLOSED else ''}")
        return True
    if code in (ABORT, UPDATING):
        log(f"The merger says: don't start the game ({'aborted' if code == ABORT else 'updating'})")
        return False
    log(f"The merger stopped unexpectedly (exit code {code})")
    msg_box(f"Smooth UI's merger stopped unexpectedly (exit code {code}).\n\nThe game starts anyway. {DETAILS}")
    return True


def start_game(command: GameCommand, log: Log) -> int:
    log(f"Starting the game (in {command.cwd or Path.cwd()})")
    try:
        code = subprocess.run(command.args, cwd=command.cwd, check=False).returncode
    except Exception as e:  # noqa: BLE001 (nothing to start)
        text = log.failure("The game can't be started", e)
        msg_box(f"The game can't be started:\n\n{text}\n\nCommand: {subprocess.list2cmdline(command.args)}\n\n"
                f"{DETAILS}", MB_ICONERROR)
        return 1
    log(f"The game exited with code {code}")
    # Windows' exit codes are unsigned 32-bit (e.g. 0xC0000005 for a crash); sys.exit takes a signed one
    return code - (1 << 32) if code >= 1 << 31 else code


def run():
    """The exe's entry point"""
    log = Log(DATA_DIR / LOG_FILE)
    try:
        code = main(sys.argv[1:], log)
    except Exception as e:  # noqa: BLE001 (a windowed exe has no console)
        text = log.failure("Unexpected error", e)
        msg_box(f"Smooth UI's launcher failed:\n\n{text}\n\n{DETAILS}", MB_ICONERROR)
        code = 1
    sys.exit(code)
