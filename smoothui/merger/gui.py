"""The merger's window (SmoothUIMerger.exe, see merger-gui.py): whether the merged patches are up to date and
what to do (the status card), the screens other mods change and notes below it, and the log when asked for.

It scans at once (state.check, the launcher's check), in a thread started before the window is even made:
on a hard disk with many mods that can take seconds, and the window shows regardless, saying it is
checking until it's done. Merge builds the package (builder.build_package) in a thread too, in a process
pool started after the window shows (and again after each merge); the card counts the screens done, and
each screen's row says when it's done. Only Tk's thread touches the widgets: threads hand their results
(and log records, and a merge's progress) over through queues, which a timer empties. What the card shows
comes from gui_texts.status_card, with the window's other texts. The merger's data folder (DATA_DIR) is passed on
explicitly, not left to the library's defaults, so that a test can point it elsewhere.

Colors and fonts are in theme.json: customtkinter's "blue" theme (its font is Roboto, which customtkinter
ships; CTkFont's "Windows" family changes it), and the window's own colors under "SmoothUI" (by tone, see
gui_texts; each a light and a dark mode color).

With --stale, the launcher opened it before starting the game (see launcher.py, for the exit codes). The
window is the same, but it comes to the front, the card offers Merge and play, Play without merging
(worded by what that costs, state.Severity) and Don't start the game, and a clean merge exits at once
(play). Closing the window means play; while a merge runs, only after a warning: the package, which may
be half written, is then deleted, and the game starts without it.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from ..common import SmoothUIError
from ..files import check_data_writable, make_dirs, rotate, stop_writes, write_file
from ..launcher import ABORT, CLOSED, PLAY
from ..log import HEADING
from ..parallel import MAX_WORKERS, kill_workers, make_pool, warm_up, worker_count
from ..paths import APP_DIR, DATA_DIR
from ..windows import MB_ICONERROR, ms_since_process_start, msg_box
from . import SMOOTHUI_URL, merger_version
from .game import (
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    Settings,
    find_games,
    game_path,
    load_settings,
    mods_settings,
    save_settings,
)
from .gui_texts import (
    COMPAT_PATCH,
    DELETE_FAILED,
    ERROR,
    FAILED,
    GLYPHS,
    HIDE_LOG,
    INFO,
    NEWER_RELEASE,
    NO_GAME,
    NOTES_HEADING,
    OK,
    PATCH_ANYWAY,
    PATCH_ANYWAY_NOTE,
    REMOVE_FAILED,
    REMOVE_QUESTION,
    REMOVE_TITLE,
    REVERT,
    REVERT_NOTE,
    SCREENS_HEADING,
    SHOW_LOG,
    STOP_MERGE,
    STOP_MERGE_TITLE,
    TEST_MODE,
    TITLE,
    UNKNOWN_FILES,
    WARNING,
    WELCOME,
    WELCOME_TITLE,
    Card,
    also_changed,
    capitalized,
    merging_status,
    row_status,
    status_card,
    unchanged_screens,
)
from .package import remove_package
from .scan import PACKAGE_FOLDER, is_package_folder, scan
from .screens import screen_label, screen_name
from .state import FileState, State, carried_force, check

log = logging.getLogger(__name__)

LOG_FILE = "merger.log"
# The full reports of the merges logged in LOG_FILE (kept in step with it: merger.1.report.json belongs to
# merger.1.log); missing when that run didn't merge.
REPORT_FILE = "merger.report.json"
THEME_FILE = Path(__file__).with_name("theme.json")
ICON_FILE = APP_DIR / "smoothui.ico"     # optional: customtkinter's own icon without it
SYMBOL_FONT = "Segoe UI Symbol"
WIDTH, HEIGHT = 920, 680                # the window's size at first, before Windows' scaling
MIN_WIDTH, MIN_HEIGHT = 720, 520
# A row's status label is at least as wide as the widest status ("Another mod loads first"), so that a status
# changing (as while merging) doesn't move the arrows after it, nor lay out the list again (it would tear)
STATUS_WIDTH = 140
BAR_DELAY = 300                         # ms before a progress bar without progress shows (most scans end first)

# Imported in main, after the scan has started: it takes a while (see the module's docstring)
ctk = None


def main() -> int:
    stale = "--stale" in sys.argv[1:]
    if not check_data_writable(DATA_DIR):
        return 1
    log_records = setup_logging()
    log.info(f"Smooth UI merger {_version()}; arguments: {subprocess.list2cmdline(sys.argv[1:]) or '(none)'}")
    settings, first_run = _settings()
    game = game_path(settings)
    if game is None:
        msg_box(NO_GAME, MB_ICONERROR)
        return CLOSED
    log.info(f"Game folder: {game}")
    for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV):
        if os.environ.get(name):
            log.info(f"Test mode: {name}={os.environ[name]}")
    calls = queue.Queue()       # (function, args) to call on Tk's thread
    start_scan(game, calls)

    global ctk
    import customtkinter as ctk  # see ctk above
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme(str(THEME_FILE))
    window = MergerWindow(game, stale, first_run, calls, log_records)
    window.root.mainloop()
    return window.exit_code


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


def _settings() -> tuple[Settings | None, bool]:
    """The merger's settings, and whether this is its first run (none, or they can't be read): then they
    are saved now, with the first game folder find_games finds (None if it finds none)"""
    try:
        settings = load_settings(DATA_DIR)
    except SmoothUIError as e:
        log.warning(f"The settings can't be read, so they are written again: {e}")
        settings = None
    if settings is not None:
        return settings, False
    # find_games(), not game_path(): that would save a test folder from SMOOTHUI_GAME_PATH
    found = find_games()
    log.info(f"Game folders found: {', '.join(str(game.path) for game in found) or 'none'}")
    if not found:
        return None, True
    settings = Settings(found[0].path)
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


def color(name):
    """One of the window's own colors (theme.json's "SmoothUI"): (light, dark)"""
    return tuple(ctk.ThemeManager.theme["SmoothUI"][name])


def font(size=13, weight="normal"):
    return ctk.CTkFont(size=size, weight=weight)


class MergerWindow:
    def __init__(self, game: Path, stale: bool, first_run: bool, calls: queue.Queue, log_records: queue.Queue):
        self.game, self.stale, self.calls, self.log_records = game, stale, calls, log_records
        # What status_card shows (see there)
        self.found = None
        self.scan_error = None
        self.merged = None
        self.merge_error = None
        self.progress = None        # the merge's (done, total), once known
        self.busy = None            # what runs in a thread: "scan", "build", "remove"
        self.card: Card | None = None
        self.busy_since = 0.0       # when it started (time.perf_counter)
        self.card_buttons = None    # the frame of the card's buttons, made again when they change
        self.card_buttons_shown = ()
        self.notices_shown = None
        self.pool = None
        self.reports = []           # merge_report for each merge, written to REPORT_FILE
        self.exit_code = CLOSED
        self.closing = False        # closed while merging: no new workers (see restart_pool)
        self.shown = False
        self.polling = False

        self.root = root = ctk.CTk()
        if ICON_FILE.exists():
            root.iconbitmap(str(ICON_FILE))     # before customtkinter sets its own, 200 ms after the window
        root.title(TITLE)
        self.center(WIDTH, HEIGHT)
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_failed
        root.bind("<Map>", self.on_map, add="+")
        root.bind("<Return>", self.press_default)
        root.grid_columnconfigure(0, weight=1)
        pad = {"padx": 16}

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(12, 0), **pad)
        ctk.CTkLabel(header, text=TITLE, font=font(20, "bold"), anchor="w").pack(fill="x")
        self.context = ctk.CTkLabel(header, text="", anchor="w", text_color=color("muted_text"))
        self.context.pack(fill="x")
        self.notices = ctk.CTkFrame(root, fg_color="transparent", height=0)
        self.notices.grid(row=1, column=0, sticky="ew", **pad)
        self.card_frame = ctk.CTkFrame(root, border_width=2, fg_color=color("card"))
        self.card_frame.grid(row=2, column=0, sticky="ew", pady=(8, 8), **pad)
        self.card_frame.grid_columnconfigure(1, weight=1)
        self.card_frame.grid_rowconfigure(4, minsize=14)    # its bottom margin
        self.card_frame.bind("<Configure>", lambda event: self.wrap_card())
        # The card's widgets are made once, and changed in place (made anew, they show half drawn for a moment)
        self.card_glyph = ctk.CTkLabel(self.card_frame, text="", font=(SYMBOL_FONT, 26), width=36)
        self.card_glyph.grid(row=0, column=0, rowspan=2, sticky="n", padx=(16, 4), pady=(14, 0))
        self.card_headline = ctk.CTkLabel(self.card_frame, text="", font=font(17, "bold"), anchor="w")
        self.card_headline.grid(row=0, column=1, sticky="ew", padx=(4, 16), pady=(14, 0))
        # Always there, also without text: so one line of it doesn't change the card's height
        self.card_text = ctk.CTkLabel(self.card_frame, text="", anchor="w", justify="left")
        self.card_text.grid(row=1, column=1, sticky="ew", padx=(4, 16), pady=(2, 0))
        self.card_bar = ctk.CTkProgressBar(self.card_frame, fg_color=color("card"), progress_color=color("card"))
        self.card_bar.grid(row=2, column=1, sticky="ew", padx=(4, 16), pady=(8, 0))
        self.screens = ctk.CTkScrollableFrame(root, fg_color="transparent")
        self.screens.grid(row=3, column=0, sticky="nsew", padx=8)
        self.screens.grid_columnconfigure(5, weight=1)
        self.screens._parent_canvas.bind("<Configure>", self.fit_scrollbar, add="+")
        self.expanded = set()       # the screens whose details show
        self.list_content = None    # what the list shows (show_screens)
        self.rows = {}              # by path: (FileState, its cells, the grid row for its details)
        self.rows_merging = False   # whether rows show a merge's progress, not their FileState
        self.details_frames = {}    # by path
        self.detail_buttons = []    # Patch anyway and Revert, disabled while something runs
        self.list_row = 0
        root.grid_rowconfigure(3, weight=1)

        self.log_view = ctk.CTkTextbox(root, wrap="word", state="disabled", height=180)
        mode = 1 if ctk.get_appearance_mode() == "Dark" else 0     # tags don't follow later changes
        for tag in (WARNING, ERROR):
            self.log_view.tag_config(tag, foreground=color(tag)[mode])
        footer = ctk.CTkFrame(root, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", pady=(4, 10), **pad)
        self.log_button = self.button(footer, SHOW_LOG, self.toggle_log, quiet=True)
        self.log_button.pack(side="left")

        self.refresh()
        # What threads hand over is shown once the window shows (redrawing it first would delay that), or
        # after a while at the latest (e.g. started minimized)
        root.after(1000, self.start_polling)
        root.after(300, self.start_pool)       # after the window shows
        if first_run:
            root.after(200, lambda: show_info(WELCOME_TITLE, WELCOME.format(game=game)))

    def center(self, width, height):
        """Size the window, and center it on the main screen (geometry's size is scaled, its position isn't)"""
        scaling = ctk.ScalingTracker.get_window_scaling(self.root)
        x = (self.root.winfo_screenwidth() - round(width * scaling)) // 2
        y = (self.root.winfo_screenheight() - round(height * scaling)) // 2
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def on_map(self, event):
        if event.widget is self.root and not self.shown:     # not its widgets' (they have the window's bind tag)
            self.shown = True
            ms = ms_since_process_start()
            log.debug(f"The window shows, {ms:.0f} ms after the merger started" if ms is not None else "The window shows")
            if self.stale:
                self.to_front()
            self.root.after_idle(self.start_polling)    # once it is drawn

    def to_front(self):
        """The launcher opened the window: the user expects the game, so it must not open behind another
        window. Windows may not give a background process the focus: on top for a moment helps."""
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def button(self, parent, text, command, quiet=False):
        """A button; quiet: drawn as a secondary one (a card's buttons but the default)"""
        if not quiet:
            return ctk.CTkButton(parent, text=text, command=command)
        return ctk.CTkButton(parent, text=text, command=command, fg_color="transparent", border_width=1,
                             border_color=color("quiet_button_border"), hover_color=color("quiet_button_hover"),
                             text_color=color("quiet_button_text"))

    # ---- Tk's thread: what threads hand over ----

    def start_polling(self):
        if not self.polling:
            self.polling = True
            self.poll()

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
        text = ("\n" if getattr(record, "heading", None) else "") + LogFormatter().format(record)
        tag = ERROR if record.levelno >= logging.ERROR else WARNING if record.levelno >= logging.WARNING else None
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n", tag)
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def toggle_log(self):
        if self.log_view.winfo_ismapped():
            self.log_view.grid_forget()
            self.root.grid_rowconfigure(4, weight=0)
            self.log_button.configure(text=SHOW_LOG)
        else:
            self.log_view.grid(row=4, column=0, sticky="nsew", padx=16, pady=(8, 0))
            self.root.grid_rowconfigure(4, weight=1)
            self.log_button.configure(text=HIDE_LOG)
            self.log_view.see("end")

    def start_pool(self):
        workers = worker_count(MAX_WORKERS)
        if workers > 1:
            self.pool = make_pool(workers)
            warm_up(self.pool, workers, "smoothui.patcher.patcher_core")

    # ---- What the window shows ----

    def refresh(self):
        """Show the window's state: the header's context line, the notices, the card, the screens"""
        installed = self.found.scan.installed if self.found else None
        context = [str(self.game)]
        if installed and installed.usable:
            context.insert(0, f"Smooth UI Remastered {installed.version.split('-')[0]}, {installed.fps} fps")
        if self.context.cget("text") != "  ·  ".join(context):
            self.context.configure(text="  ·  ".join(context))
        self.show_notices(installed)
        self.show_card()
        self.show_screens()

    def show_notices(self, installed):
        """The notices, made again only when they change"""
        notices = []
        if names := [name for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV) if os.environ.get(name)]:
            notices.append(TEST_MODE.format(names=" and ".join(names)))
        if installed and installed.newer_release:
            notices.append(NEWER_RELEASE.format(version=installed.newer_release))
        if notices == self.notices_shown:
            return
        self.notices_shown = notices
        for child in self.notices.winfo_children():
            child.destroy()
        for text in notices:
            banner = ctk.CTkFrame(self.notices, fg_color=color("notice"))
            banner.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(banner, text=GLYPHS[INFO], font=(SYMBOL_FONT, 14), text_color=color(INFO)).pack(
                side="left", padx=(10, 6), pady=4)
            ctk.CTkLabel(banner, text=text, anchor="w").pack(side="left", fill="x", pady=4)

    def show_card(self):
        """The card for the window's state (see gui_texts.status_card), changing its widgets in place. Its
        layout doesn't change while something runs, so the list below doesn't move: the progress bar is
        always there (hidden in the card's color), and a card without buttons keeps the last card's,
        disabled."""
        card = status_card(self.found, stale=self.stale, busy=self.busy, scan_error=self.scan_error,
                           merged=self.merged, merge_error=self.merge_error, progress=self.progress)
        if card == self.card:
            self.enable_buttons(True)
            return
        self.card = card
        frame = self.card_frame
        frame.configure(border_color=color(card.tone))
        self.card_glyph.configure(text=GLYPHS[card.tone], text_color=color(card.tone))
        self.card_headline.configure(text=card.headline)
        self.card_text.configure(text=" ".join(card.sentences))
        self.show_bar(card)
        self.wrap_card()

        actions = self.actions()
        buttons = tuple((label, action) for label, action in card.buttons if action in actions)
        if card.busy and not buttons:
            self.enable_buttons(False)
            return
        if buttons != self.card_buttons_shown:
            self.card_buttons_shown = buttons
            old = self.card_buttons
            self.card_buttons = None
            if buttons:     # (an empty frame would be customtkinter's 200 pixels high)
                self.card_buttons = row = ctk.CTkFrame(frame, fg_color="transparent")
                for index, (label, action) in enumerate(buttons):
                    button = self.button(row, label, actions[action], quiet=index > 0)
                    if self.stale:      # stacked, at full width, so that each always fits
                        button.pack(fill="x", pady=3)
                    else:               # at the right, the default last
                        button.pack(side="right", padx=(8, 0))
                row.grid(row=3, column=0 if self.stale else 1, columnspan=2 if self.stale else 1, sticky="ew",
                         padx=16, pady=(4, 0))
            if old is not None:     # after the new ones are placed: else the card shows none for a moment
                old.destroy()
        self.enable_buttons(True)

    def show_bar(self, card):
        """The card's progress bar: shown while something runs; without progress, only after BAR_DELAY"""
        bar = self.card_bar
        if not card.busy:
            bar.stop()
            bar.configure(fg_color=color("card"), progress_color=color("card"))
            return
        if card.progress is None:
            bar.configure(mode="indeterminate")
            bar.start()
        else:
            bar.stop()
            bar.configure(mode="determinate")
            bar.set(card.progress)
        delay = 0 if card.progress is not None else BAR_DELAY - round((time.perf_counter() - self.busy_since) * 1000)

        def show():
            if self.card is card:
                theme = ctk.ThemeManager.theme["CTkProgressBar"]
                bar.configure(fg_color=theme["fg_color"], progress_color=theme["progress_color"])
        if delay > 0:
            self.root.after(delay, show)
        else:
            show()

    def enable_buttons(self, on):
        """The card's buttons (on or off), and those in the screens' details (also off while something runs)"""
        for button in self.card_buttons.winfo_children() if self.card_buttons is not None else []:
            button.configure(state="normal" if on else "disabled")
        for button in self.detail_buttons:
            if button.winfo_exists():
                button.configure(state="normal" if on and not self.busy else "disabled")

    def wrap_card(self):
        """Wrap the card's sentences at its width (wraplength is in unscaled pixels, as sizes are)"""
        if self.card_text.winfo_exists():
            scaling = ctk.ScalingTracker.get_widget_scaling(self.card_frame)
            width = self.card_frame.winfo_width() / scaling - 36 - 56
            self.card_text.configure(wraplength=max(width, 200))

    def press_default(self, event):
        actions = self.actions()
        buttons = [action for _, action in self.card.buttons if action in actions] if self.card else []
        if buttons:
            actions[buttons[0]]()

    def actions(self):
        """What the card's buttons do, by action (see gui_texts.Card); a button whose action isn't here
        isn't shown"""
        return {
            "merge": self.merge,
            "merge_and_play": lambda: self.merge(play=True),
            "play": lambda: self.exit(PLAY),
            "abort": lambda: self.exit(ABORT),
            "remove": self.remove,
            "remove_and_play": lambda: self.remove(play=True),
            "rescan": self.rescan,
            "smoothui_page": lambda: webbrowser.open(SMOOTHUI_URL),
        }

    def show_screens(self):
        """The screens other mods change, by mod, then the line for the others, then notes. Rebuilding the list
        takes a moment, during which it shows blank: so not while it would show the same."""
        if self.rows_merging and self.busy:     # the scan after a merge: the rows show the merge's until it ends
            return
        states = self.found.states if self.found else []
        shown = sorted((state for state in states if state.state != State.NOTHING_TO_DO), key=_row_order)
        others = len(states) - len(shown)
        notes = self.notes() if self.found else []
        content = ([(state, self.found.scan.files[state.path].shippers) for state in shown], others, notes)
        merging, self.rows_merging = self.rows_merging, False
        if content == self.list_content:
            if merging:     # back from the merge's progress to what the check says
                for state in shown:
                    self.show_row_status(state.path, *row_status(state))
            return
        same_rows = (self.list_content is not None and self.list_content[1:] == content[1:]
                     and [state.path for state, _ in self.list_content[0]] == [state.path for state in shown])
        self.list_content = content
        if same_rows:       # the same screens: only what they show changes
            for state in shown:
                self.update_row(state)
            return
        for child in self.screens.winfo_children():
            child.destroy()
        self.rows, self.details_frames, self.detail_buttons = {}, {}, []
        if not (shown or others or notes):
            self.screens.grid_remove()      # an empty list would still show its scroll bar
            return
        self.screens.grid()
        self.list_row = 0
        if shown or others:
            self.list_heading(SCREENS_HEADING)
        for state in shown:
            self.screen_row(state)
        if others:
            self.list_line(unchanged_screens(others))
        if notes:
            self.list_heading(NOTES_HEADING)
            for text, tone in notes:
                self.list_line(text, GLYPHS[tone], tone)
        self.root.after_idle(self.fit_scrollbar)

    def list_heading(self, text):
        ctk.CTkLabel(self.screens, text=text, font=font(14, "bold"), anchor="w").grid(
            row=self.list_row, column=0, columnspan=6, sticky="ew", padx=8, pady=(10 if self.list_row else 0, 4))
        self.list_row += 1

    def list_line(self, text, glyph="", tone=None):
        ctk.CTkLabel(self.screens, text=glyph, font=(SYMBOL_FONT, 13), width=20,
                     text_color=color(tone) if tone else None).grid(row=self.list_row, column=0, sticky="nw", padx=(8, 4))
        ctk.CTkLabel(self.screens, text=text, anchor="w", justify="left", wraplength=760).grid(
            row=self.list_row, column=1, columnspan=5, sticky="ew", pady=(4, 0))
        self.list_row += 1

    def screen_row(self, state: FileState):
        """A screen's row: its glyph, name, mod and status; a click shows its details, in the grid row below
        it, kept for them"""
        tone, status = row_status(state)
        cells = [
            ctk.CTkLabel(self.screens, text=GLYPHS[tone], font=(SYMBOL_FONT, 13), text_color=color(tone), width=20),
            ctk.CTkLabel(self.screens, text=screen_name(state.path), anchor="w"),
            ctk.CTkLabel(self.screens, text=state.winner or "", anchor="w", text_color=color("muted_text")),
            ctk.CTkLabel(self.screens, text=status, anchor="w", width=STATUS_WIDTH),
            ctk.CTkLabel(self.screens, text="▸", font=(SYMBOL_FONT, 16), text_color=color("muted_text")),
        ]
        for column, cell in enumerate(cells):
            cell.grid(row=self.list_row, column=column, sticky="w", pady=1, padx=(8, 4) if column == 0 else (0, 16))
            cell.configure(cursor="hand2")
            cell.bind("<Button-1>", lambda event, path=state.path: self.toggle_row(path))
        self.rows[state.path] = (state, cells, self.list_row + 1)
        self.list_row += 2
        if state.path in self.expanded:
            self.show_details(state.path)

    def update_row(self, state: FileState):
        """A screen's row (and its details, if open) for its new state"""
        _, cells, row = self.rows[state.path]
        self.rows[state.path] = (state, cells, row)
        self.show_row_status(state.path, *row_status(state))
        cells[2].configure(text=state.winner or "")
        if frame := self.details_frames.pop(state.path, None):
            frame.destroy()
            self.show_details(state.path)

    def show_row_status(self, path, tone, status):
        cells = self.rows[path][1]
        cells[0].configure(text=GLYPHS[tone], text_color=color(tone))
        cells[3].configure(text=status)

    def toggle_row(self, path):
        """Show or hide a screen's details (only those: rebuilding the list would blank it for a moment)"""
        self.expanded ^= {path}
        if path in self.expanded:
            self.show_details(path)
        elif frame := self.details_frames.pop(path, None):
            frame.destroy()
            self.rows[path][1][4].configure(text="▸")
        self.root.after_idle(self.fit_scrollbar)

    def show_details(self, path):
        """Below a screen's row: its file name, the full message, the other mods that change it, and Patch
        anyway (a screen left out for its rescale's problems, never for a failure) or its revert"""
        state, cells, row = self.rows[path]
        cells[4].configure(text="▾")
        self.details_frames[path] = frame = ctk.CTkFrame(self.screens, fg_color=color("card"))
        wrap = max(self.screens.winfo_width() / ctk.ScalingTracker.get_widget_scaling(self.screens), 600) - 140
        lines = [(screen_label(path), color("muted_text")), (capitalized(state.message), None)]
        if also := also_changed(self.found.scan.files[path], state.winner):
            lines.append((also, None))
        for line, text_color in lines:
            ctk.CTkLabel(frame, text=line, anchor="w", justify="left", wraplength=wrap, text_color=text_color).pack(
                fill="x", padx=12, pady=(6, 0))
        if state.state == State.NOT_HELD and state.cause == "left out":
            button, note = PATCH_ANYWAY, PATCH_ANYWAY_NOTE
            command = lambda: self.merge(self.stale, patch_anyway=path)
        elif state.state == State.UP_TO_DATE and state.cause == "forced":
            button, note = REVERT, REVERT_NOTE.format(mod=state.winner)
            command = lambda: self.merge(self.stale, revert=path)
        else:
            button = None
        if button:
            buttons = ctk.CTkFrame(frame, fg_color="transparent")
            buttons.pack(fill="x", padx=12, pady=(8, 0))
            widget = self.button(buttons, button, command, quiet=True)
            widget.pack(side="left")
            widget.configure(state="disabled" if self.busy else "normal")
            self.detail_buttons.append(widget)
            ctk.CTkLabel(buttons, text=note, anchor="w").pack(side="left", padx=10)
        ctk.CTkFrame(frame, fg_color="transparent", height=8).pack()
        frame.grid(row=row, column=1, columnspan=5, sticky="ew", padx=(0, 16), pady=(2, 8))     # once filled

    def fit_scrollbar(self, event=None):
        """Show the list's scroll bar only when the list doesn't fit (customtkinter always shows it; its
        internals, as of customtkinter 6.0.0)"""
        bar = self.screens._scrollbar
        if self.screens.winfo_reqheight() > self.screens._parent_canvas.winfo_height():
            bar.grid()
        else:
            bar.grid_remove()

    def notes(self) -> list[tuple[str, str]]:
        """(text, tone): the scan's problems, the compat patches, and the last merge's problems and notes"""
        result = self.found.scan
        notes = [(problem.message, WARNING) for problem in result.problems]
        if self.merged is not None:
            notes += [(problem, ERROR) for problem in self.merged.check_problems]
            notes += [(note, INFO) for note in self.merged.notes]
            if unknown := self.merged.installation.written.unknown_files:
                notes.append((UNKNOWN_FILES.format(names=", ".join(unknown)), INFO))
        else:
            notes += [(COMPAT_PATCH.format(folder=folder), INFO) for folder in result.compat_patches]
        return [(capitalized(text), tone) for text, tone in notes]

    # ---- The scan ----

    def set_busy(self, what):
        """What runs in a thread now ("scan", "build", "remove"; None: nothing)"""
        if what and not self.busy:
            self.busy_since = time.perf_counter()
        self.busy = what

    def scanning(self):
        """A scan started. The window's first shows at once; a later one (Re-scan, after a merge) only if it
        takes a while: most end first, and showing it would make the window flicker"""
        first = self.found is None and self.scan_error is None
        self.set_busy("scan")
        if first:
            self.refresh()
            return
        self.enable_buttons(False)
        self.root.after(BAR_DELAY, lambda: self.busy == "scan" and self.refresh())

    def scanned(self, result, error):
        self.set_busy(None)
        if error is not None:
            self.found, self.scan_error = None, _error_text(error)
            log.error(f"The scan failed: {self.scan_error}")
        else:
            (self.found, elapsed), self.scan_error = result, None
            log.info(f"Scanned in {elapsed:.2f} s: {self.found.scan.installed.message}"
                     + (f"; {len(self.found.needs_build)} screen(s) to merge" if self.found.needs_build else ""))
            if not self.found.scan.installed.usable:
                log.warning(f"Smooth UI: {self.found.scan.installed.message}")
            for problem in self.found.scan.problems:
                log.warning(problem.message)
        self.refresh()

    def rescan(self, keep_result=False):
        """Scan again; the last merge's result stays only with keep_result (right after that merge)"""
        if not keep_result:
            self.merged = self.merge_error = None
        start_scan(self.game, self.calls)

    # ---- Merge, remove ----

    def merge(self, play=False, patch_anyway=None, revert=None):
        """Build the package in a thread, then scan again; with play (stale mode), a clean build plays at once.
        patch_anyway: a file to force too; revert: a forced file not to force (see build)"""
        self.set_busy("build")
        self.merged = self.merge_error = self.progress = None
        self.refresh()
        for path in self.rows:      # each says "Done" when its file is (see merged_file)
            if self.found.scan.files[path].winner:
                self.show_row_status(path, *merging_status(None))
                self.rows_merging = True
        log.info("Merging", extra=HEADING)

        def finished(result, error):
            self.set_busy(None)
            self.progress = None
            if error is not None:
                self.merge_error = _error_text(error)
                log.error(f"Merging failed: {self.merge_error}")
            else:
                self.merged = result
                if play and result.clean:
                    self.exit(PLAY)
                    return
            self.rescan(keep_result=True)
        in_thread(self.calls, self.build, finished, play, patch_anyway, revert)

    def build(self, play=False, patch_anyway=None, revert=None):
        """Build the package, forcing what the last build forced (the same copies), and patch_anyway, but
        not revert. Its progress goes to the window as it goes (merged_file, progressed). Then new workers
        for the next merge, unless the game starts now (play, and the build is clean)."""
        # Imported here: the patcher takes a while to import, and only a build needs it
        from ..build.shared import print_file_result
        from .builder import build_package, merge_report
        from .cli import show_build

        def on_file(path, status, *args, **kwargs):
            print_file_result(path, status, *args, **kwargs)    # the log, as without on_file
            self.calls.put(("merged_file", (path, status)))

        start = time.perf_counter()
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        settings = mods_settings()
        force, notes = carried_force(scan(self.game, settings))
        for note in notes:
            log.warning(note)
        if force:
            log.info(f"Forced again, as at the last build (the same copies): {', '.join(force)}")
        if patch_anyway:
            log.info(f"Patching {screen_label(patch_anyway)} anyway, as you chose")
            force.append(patch_anyway)
        if revert in force:
            log.info(f"Reverting {screen_label(revert)} to the unpatched screen, as you chose")
            force.remove(revert)
        result = None
        try:
            result = build_package(self.game, settings, force=force, pool=self.pool, data_dir=DATA_DIR,
                                   on_file=on_file, progress=lambda *progress: self.calls.put(("progressed", progress)))
        finally:
            if not (play and result is not None and result.clean):
                self.restart_pool()
        self.reports.append(merge_report(result, started))
        try:
            write_file(DATA_DIR / REPORT_FILE, json.dumps(self.reports, indent=1).encode("utf-8"))
            log.info(f"The full report of this merge (started {started}) is in data\\{REPORT_FILE}")
        except SmoothUIError as e:
            log.warning(f"The full report can't be written: {e}")
        show_build(result)
        log.info(f"Built in {time.perf_counter() - start:.2f} s" + ("" if result.clean else "; see the problems above"))
        return result

    def progressed(self, done, total):
        """A merge's progress (build_package's progress)"""
        if self.busy == "build":
            self.progress = (done, total)
            self.show_card()

    def merged_file(self, path, status):
        """A file is patched (build_package's on_file): its row says so until the scan after the merge"""
        if self.busy == "build" and path in self.rows:
            self.show_row_status(path, *merging_status(status))

    def restart_pool(self):
        """Replace the pool, in the build's thread (starting workers takes a moment): its workers keep the
        memory the merge used (Python doesn't give it back to Windows), and a pool whose worker died
        can't be used again. Not when the window is closing: killing the workers made the build fail, and
        new ones would outlive the merger."""
        if self.pool is None or self.closing:
            return
        start = time.perf_counter()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.start_pool()
        log.debug(f"New workers started for the next merge in {time.perf_counter() - start:.2f} s")

    def remove(self, play=False):
        """Remove the package (after asking, unless play: stale mode's Remove them and play), then scan
        again, or play"""
        if not play and not ask_yes_no(REMOVE_TITLE, REMOVE_QUESTION):
            return
        self.set_busy("remove")
        self.refresh()
        folder = next(mod.folder for mod in self.found.scan.order if is_package_folder(mod.folder))

        def work():
            removed, unknown = remove_package(self.found.scan.mods_dir / folder)
            for name in unknown:
                log.warning(f"{folder}\\{name} wasn't written by the merger, so it stays")
            log.info(f"Removed the package{'' if removed else ' (its folder stays)'}")

        def finished(result, error):
            self.set_busy(None)
            if error is not None:
                log.error(f"The package can't be removed: {_error_text(error)}")
                show_error(REMOVE_TITLE, REMOVE_FAILED.format(error=_error_text(error)))
            elif play:
                self.exit(PLAY)
                return
            self.rescan()
        in_thread(self.calls, work, finished)

    # ---- Closing ----

    def close(self):
        if self.busy == "build":
            if not ask_yes_no(STOP_MERGE_TITLE, STOP_MERGE[self.stale], icon="warning"):
                return
            log.warning("Closed while merging: deleting the package" + ("; the game starts" if self.stale else ""))
            self.closing = True
            if self.pool is not None:
                kill_workers(self.pool)
            self.delete_package_now()
            if self.pool is not None:   # again: the build's thread may have started new ones just before
                kill_workers(self.pool)
            logging.shutdown()
            os._exit(PLAY if self.stale else CLOSED)    # without waiting for the build's thread or the pool
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
        msg_box(DELETE_FAILED.format(error=error, folder=package_dir), MB_ICONERROR)

    def exit(self, code):
        log.info(f"Exit code {code}")
        self.exit_code = code
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def callback_failed(self, kind, error, tb):
        """Tk's handler for an exception in a callback (it would only print it)"""
        log.error("".join(traceback.format_exception(kind, error, tb)).rstrip())
        msg_box(FAILED.format(error=f"{kind.__name__}: {error}", log=LOG_FILE), MB_ICONERROR)


def _row_order(state: FileState):
    """By status (errors, then warnings, then the rest), then by mod (a screen no other mod changes now, e.g.
    its mod was removed, first), then by screen"""
    rank = {ERROR: 0, WARNING: 1, INFO: 2, OK: 3}[row_status(state)[0]]
    return rank, (state.winner or "").lower(), screen_name(state.path).lower()


def show_info(title, text):
    from tkinter import messagebox  # Tk is imported by now
    messagebox.showinfo(title, text)


def show_error(title, text):
    from tkinter import messagebox
    messagebox.showerror(title, text)


def ask_yes_no(title, text, icon="question") -> bool:
    from tkinter import messagebox
    return messagebox.askyesno(title, text, icon=icon, default="no")
