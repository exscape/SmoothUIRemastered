"""The merger's window (SmoothUIMerger.exe, see merger-gui.py): how it starts, and what runs in threads. The
window itself is in window.py.

It scans at once (state.check, the launcher's check), in a thread started before Qt is even imported: on a
hard disk with many mods that can take seconds, and the window shows regardless, saying it is checking until
it's done. Merging (builder.build_package) runs in a thread too, in a process pool started after the window
shows. Only Qt's thread touches the widgets: threads hand their results (and log records, and a merge's
progress) over through queues, which a timer empties (window.MergerWindow.poll). The merger's data folder
(DATA_DIR) is passed on explicitly, not left to the library's defaults, so that a test can point it elsewhere.

With --stale, the launcher opened it before starting the game (see launcher.py, for the exit codes).
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
from ..paths import DATA_DIR
from . import merger_version
from .game import (
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    Settings,
    game_path,
    load_settings,
    mods_settings,
)
from .state import check

log = logging.getLogger(__name__)

LOG_FILE = "merger.log"
# The full reports of the merges logged in LOG_FILE (kept in step with it: merger.1.report.json belongs to
# merger.1.log); missing when that run didn't merge.
REPORT_FILE = "merger.report.json"


def main() -> int:
    stale = "--stale" in sys.argv[1:]
    if not check_data_writable(DATA_DIR):
        return 1
    log_records = setup_logging()
    log.info(f"Smooth UI merger {_version()}; arguments: {subprocess.list2cmdline(sys.argv[1:]) or '(none)'}")
    settings = _settings()
    game = game_path(settings)
    log.info(f"Game folder: {game or 'not chosen yet'}")
    for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV):
        if os.environ.get(name):
            log.info(f"Test mode: {name}={os.environ[name]}")
    calls = queue.Queue()       # (function, args) to call on Qt's thread
    if game is not None:        # on the first run, once the welcome page knows the folders found
        start_scan(game, calls)

    from . import window  # imports Qt, which takes a while: after the scan has started
    return window.run(game, stale, settings is None, calls, log_records)


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


def _version():
    try:
        return merger_version()
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


def error_text(error) -> str:
    return str(error) if isinstance(error, SmoothUIError) else f"{type(error).__name__}: {error}"
