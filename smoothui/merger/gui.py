"""The merger's window (SmoothUIMerger.exe, see merger-gui.py): a test version, until the real GUI. Plain
on purpose: the scan's result, a log, and buttons.

It scans at once (state.check, the launcher's check), in a thread started before the window is even made:
on a hard disk with many mods that can take seconds, and the window shows regardless, saying it is
scanning until it's done. Merge builds the package (builder.build_package) in a thread too, in a process
pool started after the window shows; the build's log shows in the window. Only Tk's thread touches the
widgets: threads hand their results (and log records) over through queues, which a timer empties. The
merger's data folder (DATA_DIR) is passed on explicitly, not left to the library's defaults, so that a
test can point it elsewhere.

With --stale, the launcher opened it before starting the game (see launcher.py, for the exit codes): a
smaller window over it says why, with Merge and play, Play without merging (worded by how much that
costs, state.Severity), and Abort game launch. Closing the window means play; while a build runs, only
after a warning: the package, which may be half written, is then deleted, and the game doesn't start.
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
from ..files import make_dirs, stop_writes
from ..launcher import ABORT, CLOSED, PLAY
from ..parallel import MAX_WORKERS, make_pool, warm_up, worker_count
from ..paths import DATA_DIR
from ..windows import MB_ICONERROR, ms_since_process_start, msg_box
from . import merger_version
from .game import (
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    Settings,
    find_game,
    game_path,
    load_settings,
    mods_settings,
    save_settings,
)
from .installed import Status
from .package import remove_package
from .scan import PACKAGE_FOLDER, is_package_folder, scan
from .state import Check, Severity, State, carried_force, check

log = logging.getLogger(__name__)

LOG_FILE = "merger.log"

# Imported in main, after the scan has started: it takes a while (see the module's docstring)
ctk = None

# Play without merging: the button, and what it costs (state.Severity)
PLAY_WITHOUT_MERGING = {
    Severity.HIGH: ("Play without merging (may break mods)",
                    "Without merging, some of your mods' screens may look wrong or not work at all."),
    Severity.MEDIUM: ("Play without merging (unpatched screens)",
                      ("Without merging, some of your mods' screens run at their original frame rate, without Smooth "
                       "UI's patches.")),
    Severity.LOW: ("Play without merging",
                   ("Your game works as it is, but merging again is recommended: this version of the merger builds "
                    "the package differently.")),
}
SEVERITY_COLORS = {Severity.HIGH: "#e05252", Severity.MEDIUM: "#e69500"}
LOG_COLORS = {"warning": "#e69500", "error": "#e05252"}

WELCOME = """Welcome to Smooth UI Remastered's merger (a test version).

Some of your other mods ship their own copies of the screens Smooth UI patches. The game then shows those copies, \
without Smooth UI's frame rate patches. The merger patches those copies, and puts them into a mod of its own \
(mod0000_SmoothUI_MergedPatches), which the game loads first.

Start the game with SmoothUILauncher.exe: it checks your mods first, and opens the merger when they changed.

Game folder (for now, always this one): {game}"""

STOP_BUILD = ("The merger is still building the package. Closing now stops it halfway, so the package is deleted "
              "(a half-written one could break the game), and the game won't start.\n\nOnly do this if the merger "
              "seems stuck.\n\nClose anyway?")


def main() -> int:
    stale = "--stale" in sys.argv[1:]
    log_records = setup_logging()
    log.info(f"Smooth UI merger {_version()}; arguments: {subprocess.list2cmdline(sys.argv[1:]) or '(none)'}")
    settings, first_run = _settings()
    game = game_path(settings)
    log.info(f"Game folder: {game}")
    calls = queue.Queue()       # (function, args) to call on Tk's thread
    start_scan(game, calls)

    global ctk
    import customtkinter as ctk  # see ctk above
    ctk.set_appearance_mode("system")
    window = MergerWindow(game, stale, first_run, calls, log_records)
    window.root.mainloop()
    return window.exit_code


def setup_logging() -> queue.Queue:
    """Log to data\\merger.log (rewritten on each start; a failure to open it is ignored) and into a queue
    for the window's log view (returned)"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("smoothui").setLevel(logging.DEBUG)
    try:
        make_dirs(DATA_DIR)
        file = logging.FileHandler(DATA_DIR / LOG_FILE, mode="w", encoding="utf-8")
    except (SmoothUIError, OSError):
        pass
    else:
        file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
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


def _settings() -> tuple[Settings, bool]:
    """The merger's settings, and whether this is its first run (none, or they can't be read): then they
    are saved now"""
    try:
        settings = load_settings(DATA_DIR)
    except SmoothUIError as e:
        log.warning(f"The settings can't be read, so they are written again: {e}")
        settings = None
    if settings is not None:
        return settings, False
    # find_game(), not game_path(): that would save a test folder from SMOOTHUI_GAME_PATH
    settings = Settings(find_game())
    try:
        save_settings(settings, DATA_DIR)
        log.info("First run: settings saved")
    except SmoothUIError as e:
        log.error(f"The settings can't be saved: {e}")
    return settings, True


def in_thread(calls: queue.Queue, work, done, *args):
    """Run work(*args) in a thread, then done(result, error) on Tk's thread (error: the exception, or
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


def _error_text(error) -> str:
    return str(error) if isinstance(error, SmoothUIError) else f"{type(error).__name__}: {error}"


class MergerWindow:
    def __init__(self, game: Path, stale: bool, first_run: bool, calls: queue.Queue, log_records: queue.Queue):
        self.game, self.stale, self.calls, self.log_records = game, stale, calls, log_records
        self.found: Check | None = None
        self.busy = None            # what runs in a thread: "scan", "build", "remove"
        self.pool = None
        self.exit_code = CLOSED
        self.shown = False

        self.root = root = ctk.CTk()
        root.title(f"Smooth UI Remastered merger {_version()} (test version)")
        root.geometry("1000x720")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_failed
        root.bind("<Map>", self.on_map, add="+")   # also its widgets' (they have the window's bind tag)

        ctk.CTkLabel(root, text=_command_line(), justify="left", anchor="w", text_color="gray").pack(
            fill="x", padx=16, pady=(10, 0))
        self.status = ctk.CTkLabel(root, text="", justify="left", anchor="w", font=ctk.CTkFont(size=18, weight="bold"))
        self.status.pack(fill="x", padx=16, pady=(6, 6))
        self.results = ctk.CTkTextbox(root, height=230, wrap="word", state="disabled")
        self.results.pack(fill="x", padx=16)

        self.buttons = ctk.CTkFrame(root, fg_color="transparent")
        self.buttons.pack(fill="x", padx=16, pady=8)
        self.rescan_button = ctk.CTkButton(self.buttons, text="Re-scan", command=self.rescan)
        self.merge_button = ctk.CTkButton(self.buttons, text="Merge", command=self.merge)
        self.remove_button = ctk.CTkButton(self.buttons, text="Remove the package", command=self.remove)
        if not stale:
            self.rescan_button.pack(side="left", padx=(0, 8))
            self.merge_button.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(root, text="Log", anchor="w").pack(fill="x", padx=16)
        self.log_view = ctk.CTkTextbox(root, wrap="word", state="disabled")
        self.log_view.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        for tag, color in LOG_COLORS.items():
            self.log_view.tag_config(tag, foreground=color)

        self.popup = StalePopup(self) if stale else None
        root.after(0, self.poll)
        root.after(300, self.start_pool)       # after the window shows
        if first_run:
            root.after(200, lambda: show_info("Welcome", WELCOME.format(game=game)))

    def on_map(self, event):
        if not self.shown:
            self.shown = True
            ms = ms_since_process_start()
            log.debug(f"The window shows, {ms:.0f} ms after the merger started" if ms is not None else "The window shows")

    # ---- Tk's thread: what threads hand over ----

    def poll(self):
        self.root.after(50, self.poll)      # first: a call below may fail
        while True:
            try:
                record = self.log_records.get_nowait()
            except queue.Empty:
                break
            self.show_log(record)
        while True:
            try:
                function, args = self.calls.get_nowait()
            except queue.Empty:
                break
            (getattr(self, function) if isinstance(function, str) else function)(*args)

    def show_log(self, record):
        text = record.getMessage()
        if getattr(record, "heading", None):
            text = f"\n== {text} =="
        tag = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else None
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n", tag)
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def start_pool(self):
        workers = worker_count(MAX_WORKERS)
        if workers > 1:
            self.pool = make_pool(workers)
            warm_up(self.pool, workers, "smoothui.patcher.patcher_core")

    # ---- The scan ----

    def scanning(self):
        self.busy = "scan"
        self.status.configure(text="Scanning your mods...", text_color=("black", "white"))
        self.set_buttons()
        if self.popup:
            self.popup.scanning()

    def scanned(self, result, error):
        self.busy = None
        if error is not None:
            self.found = None
            self.status.configure(text=f"The scan failed: {_error_text(error)}", text_color=LOG_COLORS["error"])
            self.show_results([f"The scan failed: {_error_text(error)}"])
        else:
            self.found, elapsed = result
            self.status.configure(text=_summary(self.found), text_color=SEVERITY_COLORS.get(self.found.severity,
                                                                                            ("black", "white")))
            self.show_results(_report(self.found) + [f"\nScanned in {elapsed:.2f} s"])
            log.info(f"Scanned in {elapsed:.2f} s: {_summary(self.found)}")
        self.set_buttons()
        if self.popup:
            self.popup.scanned(self.found, error)

    def show_results(self, lines):
        self.results.configure(state="normal")
        self.results.delete("1.0", "end")
        self.results.insert("1.0", "\n".join(lines))
        self.results.configure(state="disabled")

    def rescan(self):
        start_scan(self.game, self.calls)

    def set_buttons(self):
        idle = self.busy is None
        self.rescan_button.configure(state="normal" if idle else "disabled")
        usable = self.found is not None and self.found.scan.installed.usable
        self.merge_button.configure(state="normal" if idle and usable else "disabled")
        if not self.stale and self.can_remove():
            self.remove_button.pack(side="left", padx=(0, 8))
            self.remove_button.configure(state="normal" if idle else "disabled")
        else:
            self.remove_button.pack_forget()

    def can_remove(self) -> bool:
        """Whether to offer removing the package: it's there, for a Smooth UI that isn't installed or is
        disabled (another build: the user updates the merger, or Smooth UI)"""
        return (self.found is not None and self.found.package_without_smoothui
                and self.found.scan.installed.status in (Status.NOT_INSTALLED, Status.DISABLED))

    # ---- Merge, remove ----

    def merge(self, done=None):
        """Build the package in a thread; then done(result, error) if given, and a new scan"""
        self.busy = "build"
        self.set_buttons()
        self.status.configure(text="Merging...", text_color=("black", "white"))

        def finished(result, error):
            self.busy = None
            if error is not None:
                log.error(f"Merging failed: {_error_text(error)}")
            self.status.configure(text="Merging failed: see the log" if error else "Merged: see the log")
            self.set_buttons()
            if done:
                done(result, error)
            else:
                self.rescan()
        in_thread(self.calls, self.build, finished)

    def build(self):
        # Imported here: the patcher takes a while to import, and only a build needs it
        from .builder import build_package
        from .cli import show_build
        start = time.perf_counter()
        settings = mods_settings()
        force, notes = carried_force(scan(self.game, settings))
        for note in notes:
            log.warning(note)
        if force:
            log.info(f"Forced again, as at the last build (the same copies): {', '.join(force)}")
        result = build_package(self.game, settings, force=force, pool=self.pool, data_dir=DATA_DIR)
        show_build(result)
        log.info(f"\nBuilt in {time.perf_counter() - start:.2f} s" + ("" if result.clean else "; see the problems above"))
        return result

    def remove(self, done=None):
        if done is None and not ask_yes_no("Remove the package?", "Remove the merger's package from your Mods "
                                           "folder? Its mods.settings entry stays."):
            return
        self.busy = "remove"
        self.set_buttons()
        folder = next(mod.folder for mod in self.found.scan.order if is_package_folder(mod.folder))

        def work():
            removed, unknown = remove_package(self.found.scan.mods_dir / folder)
            for name in unknown:
                log.warning(f"{folder}\\{name} wasn't written by the merger, so it stays")
            log.info(f"Removed the package{'' if removed else ' (its folder stays)'}")

        def finished(result, error):
            self.busy = None
            if error is not None:
                log.error(f"The package can't be removed: {_error_text(error)}")
            if done:
                done(result, error)
            else:
                self.rescan()
        in_thread(self.calls, work, finished)

    # ---- Closing ----

    def close(self):
        if self.busy == "build":
            if not ask_yes_no("Stop merging?", STOP_BUILD, icon="warning"):
                return
            log.warning("Closed while building: deleting the package; the game doesn't start")
            self.delete_package_now()
            logging.shutdown()
            os._exit(ABORT)     # without waiting for the build's thread or the pool
        self.exit(CLOSED)

    def delete_package_now(self):
        """Delete the package a build is writing (all the merger's files in it, see package.remove_package),
        while that build's thread can't be stopped: block its writes first; a file it is writing now can't
        be deleted until it's done, so try again for a while. A second pass after that catches a write
        that started just before the block."""
        stop_writes()
        result = self.found.scan
        folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), PACKAGE_FOLDER)
        package_dir = result.mods_dir / folder

        def delete():
            # Returns the error if it still fails after a while
            for _ in range(20):
                try:
                    remove_package(package_dir)
                    return None
                except SmoothUIError as e:
                    error = e
                    time.sleep(0.25)
            return error

        error = delete()
        if error is None:
            time.sleep(0.3)
            error = delete()
        if error is None:
            log.info(f"Deleted the merger's files in {package_dir}")
            return
        log.error(f"The package can't be deleted: {error}")
        msg_box(f"The package can't be deleted: {error}\n\nDelete the folder {package_dir} yourself before you play.",
                MB_ICONERROR)

    def exit(self, code):
        log.info(f"Exit code {code}")
        self.exit_code = code
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def callback_failed(self, kind, error, tb):
        """Tk's handler for an exception in a callback (it would only print it)"""
        log.error("".join(traceback.format_exception(kind, error, tb)).rstrip())
        msg_box(f"Smooth UI's merger failed:\n\n{kind.__name__}: {error}\n\nDetails are in data\\{LOG_FILE}.",
                MB_ICONERROR)


class StalePopup:
    """Stale mode's window: why the merger opened, and what to do"""
    def __init__(self, window: MergerWindow):
        self.window = window
        self.top = top = ctk.CTkToplevel(window.root)
        top.title("Smooth UI: before the game starts")
        top.geometry("620x430")
        top.transient(window.root)
        top.protocol("WM_DELETE_WINDOW", window.close)
        top.after(100, self.place)

        ctk.CTkLabel(top, text="Before the game starts", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=20, pady=(16, 4))
        self.text = ctk.CTkLabel(top, text="", justify="left", anchor="w", wraplength=570)
        self.text.pack(fill="x", padx=20)
        self.cost = ctk.CTkLabel(top, text="", justify="left", anchor="w", wraplength=570)
        self.cost.pack(fill="x", padx=20, pady=(8, 0))
        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", padx=20, pady=16)
        self.merge_button = ctk.CTkButton(buttons, text="Merge and play", command=self.merge_and_play)
        self.remove_button = ctk.CTkButton(buttons, text="Remove the package and play", command=self.remove_and_play)
        self.play_button = ctk.CTkButton(buttons, text="Play without merging", fg_color="gray40",
                                         command=lambda: window.exit(PLAY))
        self.abort_button = ctk.CTkButton(buttons, text="Abort game launch", fg_color="gray40",
                                          command=lambda: window.exit(ABORT))
        self.show_buttons(play="Play without merging")

    def place(self):
        """Over the middle of the main window, so a bit of it shows around"""
        root = self.window.root
        self.top.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - self.top.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - self.top.winfo_height()) // 3
        self.top.wm_geometry(f"+{max(x, 0)}+{max(y, 0)}")    # wm_geometry: in pixels, as winfo gives them
        self.top.lift()
        self.top.focus_force()

    def show_buttons(self, *, merge=False, remove=False, play=None, color=None, abort=True):
        for button in (self.merge_button, self.remove_button, self.play_button, self.abort_button):
            button.pack_forget()
        if merge:
            self.merge_button.pack(fill="x", pady=3)
        if remove:
            self.remove_button.pack(fill="x", pady=3)
        if play:
            self.play_button.configure(text=play, fg_color=color or "gray40")
            self.play_button.pack(fill="x", pady=3)
        if abort:
            self.abort_button.pack(fill="x", pady=3)

    def say(self, text, cost="", color=None):
        self.text.configure(text=text)
        self.cost.configure(text=cost, text_color=color or ("black", "white"))

    def scanning(self):
        self.say("Checking your mods...")
        self.show_buttons(play="Play without merging")

    def scanned(self, found: Check | None, error):
        if error is not None:
            self.say(f"The merger can't check your mods: {_error_text(error)}")
            self.show_buttons(play="Play")
        elif found.package_without_smoothui:
            self.say(f"{found.scan.installed.message}.\n\nThe merger's package was made for a Smooth UI build that "
                     "can't be used now.", "With the package as it is, some of your mods' screens may look wrong or "
                     "not work at all.", SEVERITY_COLORS[Severity.HIGH])
            self.show_buttons(remove=self.window.can_remove(), play="Play with the package as it is",
                              color=SEVERITY_COLORS[Severity.HIGH])
        elif found.stale:
            lines = [f"- {Path(state.path).name}: {state.message}" for state in found.states if state.severity]
            button, cost = PLAY_WITHOUT_MERGING[found.severity]
            self.say("Smooth UI's patches for your other mods are out of date:\n\n" + "\n".join(lines), cost,
                     SEVERITY_COLORS.get(found.severity))
            self.show_buttons(merge=True, play=button, color=SEVERITY_COLORS.get(found.severity))
        else:
            self.say("Nothing to merge: the package is up to date now.")
            self.show_buttons(play="Play")

    def merge_and_play(self):
        self.say("Merging... (the log is in the window behind this one)")
        self.show_buttons(abort=False)      # closing the window warns first
        self.window.merge(done=self.merged)

    def merged(self, result, error):
        if error is not None:
            self.say(f"Merging failed: {_error_text(error)}\n\nDetails are in the log behind this window.")
            self.show_buttons(merge=True, play="Play without merging")
        elif result.clean:
            self.window.exit(PLAY)
        else:
            self.say("Merged, with problems: see the log behind this window. Files the merger couldn't patch "
                     "are left out of the package, so the game shows those mods' own copies, unpatched.")
            self.show_buttons(play="Play")

    def remove_and_play(self):
        self.say("Removing the package...")
        self.show_buttons(abort=False)

        def removed(result, error):
            if error is None:
                self.window.exit(PLAY)
            else:
                self.say(f"The package can't be removed: {_error_text(error)}")
                self.show_buttons(remove=True, play="Play with the package as it is")
        self.window.remove(done=removed)


def show_info(title, text):
    from tkinter import messagebox  # Tk is imported by now
    messagebox.showinfo(title, text)


def ask_yes_no(title, text, icon="question") -> bool:
    from tkinter import messagebox
    return messagebox.askyesno(title, text, icon=icon, default="no")


def _command_line():
    lines = [f"Command line: {subprocess.list2cmdline(sys.argv[1:]) or '(no arguments)'}"]
    lines += [f"{name}={os.environ[name]}" for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV) if os.environ.get(name)]
    return "   ".join(lines)


def _summary(found: Check) -> str:
    installed = found.scan.installed
    if found.package_without_smoothui:
        return f"The package doesn't fit: {installed.message}"
    if not installed.usable:
        return installed.message
    if found.stale:
        return (f"A merge is needed: {len(found.needs_build)} file(s) (playing without merging: "
                f"{found.severity.name.lower()})")
    return "Up to date: nothing to merge"


def _report(found: Check) -> list[str]:
    """The scan's result, as merge.py --scan shows it (shorter)"""
    result = found.scan
    lines = [f"Smooth UI: {result.installed.message}", ""]
    shown = [state for state in found.states if state.state != State.NOTHING_TO_DO]
    for state in shown:
        severity = f" ({state.severity.name.lower()})" if state.severity else ""
        lines.append(f"{state.path}: {state.state.value}{severity}: {state.message}")
    others = len(found.states) - len(shown)
    if others:
        lines.append(f"{others} more file(s): no other mod ships them, so there is nothing to merge")
    notes = [problem.message for problem in result.problems]
    notes += [f"{folder} is a Smooth UI compat patch: the package replaces it for every file the package holds"
              for folder in result.compat_patches]
    if notes:
        lines += ["", "Problems:", *notes]
    return lines
