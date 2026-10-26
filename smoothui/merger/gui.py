"""The merger's window (SmoothUIMerger.exe, see merger-gui.py): how it starts, and what runs in threads. The
window itself is in window.py.

It scans at once (state.check, the launcher's check), in a thread started before Qt is even imported: on a
hard disk with many mods that can take seconds, and the window shows regardless, saying it is checking until
it's done. Merging (builder.build_package) runs in a thread too, in a process pool started after the window
shows. Only Qt's thread touches the widgets: threads hand their results (and log records, and a merge's
progress) over through queues, which a timer empties (window.MergerWindow.poll). The merger's data folder
(DATA_DIR) is passed on explicitly, not left to the library's defaults, so that a test can point it elsewhere.

With --stale, the launcher opened it before starting the game (see launcher.py, for the exit codes).

An update (self_update.py) runs this exe from the unpacked new version as --finish-update, which does its work
before anything else here; the new merger it starts has --update-done OLD_VERSION (and --critical, for a
critical update).

Started by Mod Organizer 2 from inside the game folder (game.mo2_inside_game), it says that this is
unsupported, and exits before it writes anything (in stale mode, the game starts).

Once a scan has ended, it looks for a newer version of the tools, and for new Smooth UI manifests, in a
thread (find_updates; see updates.py), unless the settings turn that off. In stale mode too, where nothing
waits for it, except for an unknown Smooth UI build: a new manifest, or a newer version of the tools, may be
what is missing, so the card says it is looking.

One merger at a time (two would write the same files): a second one brings the first one's window to the
front and exits at once, without touching the data folder (rotating the logs would cut the first one's).
Started by the launcher, it exits with ABORT: the game mustn't start while the user may merge in the
window that is open.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from ..common import SmoothUIError
from ..files import check_data_writable, make_dirs, rotate
from ..launcher import ABORT, CLOSED, PLAY
from ..paths import APP_DIR, DATA_DIR, FROZEN
from ..windows import MB_ICONERROR, bring_to_front, find_window, msg_box, only_instance
from . import UPDATE_TOKEN_ENV, UPDATE_URL_ENV, self_update, tools_version
from .game import (
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    Settings,
    game_path,
    load_settings,
    mo2_inside_game,
    mods_settings,
)
from .gui_texts import MO2_INSIDE_GAME, TITLE
from .state import check

log = logging.getLogger(__name__)

LOG_FILE = "merger.log"
# The full reports of the merges logged in LOG_FILE (kept in step with it: merger.1.report.json belongs to
# merger.1.log); missing when that run didn't merge.
REPORT_FILE = "merger.report.json"
# Held while a merger runs (see only_instance)
MUTEX = "SmoothUIMerger"
# The start of the window class of Qt's windows, to find the window of another merger
QT_WINDOW_CLASS = "Qt"


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--finish-update"]:     # (before everything: the old merger may still hold the mutex)
        return self_update.finish(args[1:])
    stale = "--stale" in args
    updated_from = args[args.index("--update-done") + 1] if "--update-done" in args[:-1] else None
    critical = updated_from is not None and self_update.CRITICAL_ARG in args
    if not only_instance(MUTEX):
        if (hwnd := find_window(TITLE, QT_WINDOW_CLASS)) is not None:
            bring_to_front(hwnd)
        return ABORT if stale else CLOSED
    if mo2_inside_game():
        msg_box(MO2_INSIDE_GAME[stale].format(folder=APP_DIR), MB_ICONERROR)
        return PLAY if stale else 1
    if not check_data_writable(DATA_DIR):
        return 1
    log_records = setup_logging()
    log.info(f"Smooth UI merger {version()}; arguments: {subprocess.list2cmdline(sys.argv[1:]) or '(none)'}")
    settings = _settings()
    game = game_path(settings)
    if updated_from:
        log.info(f"Updated from Smooth UI Tools v{updated_from} to v{version()}" + (" (critical)" if critical else ""))
    log.info(f"Game folder: {game or 'not chosen yet'}")
    for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV, UPDATE_URL_ENV, UPDATE_TOKEN_ENV):
        if os.environ.get(name):
            value = "(set; not logged)" if name == UPDATE_TOKEN_ENV else os.environ[name]
            log.info(f"Test mode: {name}={value}")
    self_update.start_cleanup(DATA_DIR)     # what an update left (it deletes data\\update\\)
    calls = queue.Queue()       # (function, args) to call on Qt's thread
    if game is not None:        # on the first run, once the welcome page knows the folders found
        start_scan(game, calls)

    from . import window  # imports Qt, which takes a while: after the scan has started
    return window.run(game, stale, settings, calls, log_records, updated_from=updated_from, critical=critical)


class LogFormatter(logging.Formatter):
    """Marks headings (log.HEADING, log.SUBHEADING) with ===== or ----- (the console draws banners instead)"""
    def formatMessage(self, record):
        mark = {1: "=====", 2: "-----"}.get(getattr(record, "heading", None))
        message = f"{mark} {record.message} {mark}" if mark else record.message
        return super().formatMessage(logging.makeLogRecord({**record.__dict__, "message": message}))


def setup_logging() -> queue.Queue:
    """Log to data\\merger.log (the last three runs kept, with their reports; a failure to open it is
    ignored) and into a queue for the window's log view (returned)"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("smoothui").setLevel(logging.DEBUG)
    try:
        make_dirs(DATA_DIR)
        rotate(DATA_DIR, [LOG_FILE, REPORT_FILE])
        file = logging.FileHandler(DATA_DIR / LOG_FILE, mode="w", encoding="utf-8")
    except (SmoothUIError, OSError):
        pass
    else:
        file.setFormatter(LogFormatter("%(asctime)s %(levelname)-7s %(message)s"))
        root.addHandler(file)
    records = queue.Queue()
    view = logging.handlers.QueueHandler(records)
    view.setLevel(logging.INFO)
    root.addHandler(view)
    return records


def version():
    try:
        return tools_version()
    except SmoothUIError as e:
        return f"unknown ({e})"


def _settings() -> Settings | None:
    """The merger's settings; None on its first run (none, or they can't be read: the welcome page writes
    them again)"""
    try:
        return load_settings(DATA_DIR)
    except SmoothUIError as e:
        log.warning(f"The settings can't be read, so the welcome page shows: {e}")
        return None


def in_thread(calls: queue.Queue, work, done, *args):
    """Run work(*args) in a thread, then done(result, error) on Qt's thread (error: the exception, or
    None). done is a function, or the name of a MergerWindow method (the window may not exist yet)."""
    def run():
        try:
            result, error = work(*args), None
        except Exception as e:  # noqa: BLE001 (handed to done)
            result, error = None, e
            if not isinstance(e, SmoothUIError):
                log.error("".join(traceback.format_exception(e)).rstrip())
        calls.put((done, (result, error)))
    threading.Thread(target=run, daemon=True).start()   # daemon: closing mid-build mustn't wait for it


def start_scan(game: Path, calls: queue.Queue):
    def work():
        start = time.perf_counter()
        found = check(game, mods_settings(), data_dir=DATA_DIR)
        return found, time.perf_counter() - start
    calls.put(("scanning", ()))
    in_thread(calls, work, "scanned")


def find_updates(waited_for: bool, last_check):
    """The update check (updates.check), in a thread; last_check: when the last one was (settings). When the
    game waits for it (stale mode, an unknown Smooth UI build): a shorter timeout, and even right after another
    check (the scan has just found something that check may not have known). None when run from source: an
    update would replace the repo's files with the tools' (and downloads aren't read from there anyway)."""
    if not FROZEN:
        log.info("Update check: skipped (run from source)")
        return None
    from . import updates  # imported here: it takes a while, and the window shows first
    if waited_for:
        return updates.check(DATA_DIR, timeout=updates.QUICK_TIMEOUT)
    return updates.check(DATA_DIR, last_check=last_check)


def error_text(error) -> str:
    return str(error) if isinstance(error, SmoothUIError) else f"{type(error).__name__}: {error}"
