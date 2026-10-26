"""The merger's window (started by gui.py): whether the merged patches are up to date and what to do (the
status card), the screens other mods change and notes below it, and the log when asked for. Qt (PySide6,
Qt Widgets), imported only once the scan has started (see gui.py).

Merge builds the package in a thread (gui.in_thread), in a process pool started after the window shows (and
again after each merge); the card counts the screens done, and each screen's row says when it's done. What
the card shows comes from gui_texts.status_card, with the window's other texts.

Its pages share the header and notices: the main page (the card, the list, the log), the welcome page on the
first run (no settings yet: data\\settings.json), the settings page, and the help page (versions, links, Save
a bug report, the licenses). The welcome and settings pages have the game folder
chooser (the folders find_games finds, or one the user picks) and a button that makes a launcher shortcut on
the desktop. On the first run, the scan starts on the folder the welcome preselects, so that its result is
ready when the user presses Continue, which saves the settings.

The look: Qt's Fusion style (the same on Windows 10 and 11, and it does dark mode), with the colors in
theme.json (each a light and a dark mode color, following Windows' setting) and Roboto, which ships in fonts\\.
A style sheet (STYLE) draws buttons, the progress bar, radio buttons and scroll bars as rounded, flat shapes,
as customtkinter did.

With --stale, the launcher opened it before starting the game. The window is the same, but it comes to the
front, the card offers Merge and play, Play without merging (worded by what that costs, state.Severity) and
Don't start the game, and a clean merge exits at once (play). Closing the window means play; while a merge
runs, only after a warning: the package, which may be half written, is then deleted, and the game starts
without it.

After the first scan, the update check (gui.find_updates) runs in a thread. A newer version of the tools
shows as a notice, with its release notes in the What's new dialog; a critical one (updates.CRITICAL) as a
taller, red notice. In stale mode, only a critical one shows, and a clean Merge and play then stays open
instead of playing; for an unknown Smooth UI build, the card offers any newer version instead (and leads with
it). New Smooth UI manifests make an unknown Smooth UI build scan again.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import time
import traceback
import webbrowser
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..common import SmoothUIError, fail
from ..files import stop_writes, write_file
from ..launcher import ABORT, CLOSED, LAUNCHER_EXE, PLAY
from ..patcher.fixes import fix_consequence, fix_kind
from ..paths import APP_DIR, FROZEN, REPO_ROOT
from ..windows import MB_ICONERROR, ms_since_process_start, msg_box, set_topmost
from . import (
    FILES_URL,
    REPORT_URL,
    SMOOTHUI_URL,
    TOOLS_README_URL,
    UPDATE_URL_ENV,
    gui,
    self_update,
    tools_version,
)
from .game import (
    GAME_EXE,
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    GameFolder,
    Settings,
    find_games,
    game_folder_of,
    game_path,
    is_game_folder,
    mods_settings,
    save_settings,
)
from .gui import LOG_FILE, REPORT_FILE, LogFormatter, error_text, in_thread, start_scan
from .gui_texts import (
    BACK,
    BUG,
    BUG_DIALOG,
    BUG_HEADING,
    BUG_REPORT_FAILED,
    BUG_REPORT_TITLE,
    CANCEL,
    CHECK_UPDATES,
    CHOOSE_FOLDER,
    CHOOSE_FOLDER_TITLE,
    CLOSE,
    COMPAT_REMOVABLE,
    CONTINUE,
    COPY_REPORT,
    CREATE_SHORTCUT,
    CRITICAL,
    CRITICAL_UPDATE,
    DATA,
    DATA_HEADING,
    DELETE_FAILED,
    DOWNLOAD_PAGE,
    ERROR,
    FAILED,
    GAME_HEADING,
    GAME_NOT_FOUND,
    GLYPHS,
    GUIDE_HEADING,
    GUIDE_LINK,
    HELP,
    HIDE_LOG,
    INFO,
    LATER,
    LAUNCHER,
    LAUNCHER_HEADING,
    LICENSES,
    LICENSES_HEADING,
    LOOKING,
    LOOKING_FOR_GAME,
    MERGER_VERSION,
    NEWER_RELEASE,
    NO_LAUNCHER,
    NOTES_HEADING,
    NOTHING_FOUND,
    OK,
    OPEN_DATA,
    OPEN_LICENSES,
    OPEN_LOG_FOLDER,
    PATCH_ANYWAY,
    PATCH_ANYWAY_NOTE,
    PATCH_ANYWAY_QUESTION,
    PATCH_ANYWAY_TITLE,
    REMOVE_FAILED,
    REMOVE_QUESTION,
    REMOVE_TITLE,
    REPORT_COPIED,
    REPORT_LINK,
    REVERT,
    REVERT_NOTE,
    SAVE,
    SAVE_ZIP,
    SCREENS_HEADING,
    SCREENS_NOTE,
    SETTINGS,
    SETTINGS_NOT_SAVED,
    SETUP_LINK,
    SHORTCUT_DESCRIPTION,
    SHORTCUT_MADE,
    SHORTCUT_NAME,
    SHOW_LOG,
    SMOOTHUI_NOT_CHECKED,
    STOP_MERGE,
    STOP_MERGE_TITLE,
    TEST_MODE,
    TITLE,
    UNKNOWN_FILES,
    UPDATE,
    UPDATE_AVAILABLE,
    UPDATE_FAILED,
    UPDATE_FAILED_TITLE,
    UPDATE_NOTE,
    UPDATE_NOW,
    UPDATED,
    VERSIONS_HEADING,
    WARNING,
    WELCOME,
    WELCOME_HEADING,
    WHATS_NEW,
    WHATS_NEW_TITLE,
    WRITE_BUG_REPORT,
    ZIP_NOTE,
    Card,
    also_changed,
    capitalized,
    left_out,
    merging_status,
    release_notes,
    row_status,
    status_card,
    unchanged_screens,
)
from .installed import Status
from .package import remove_package
from .scan import PACKAGE_FOLDER, is_package_folder, scan
from .screens import screen_label, screen_name
from .shortcut import create_shortcut, desktop_folder
from .state import FileState, State, carried_force

log = logging.getLogger(__name__)

THEME = json.loads(Path(__file__).with_name("theme.json").read_text(encoding="utf-8"))
FONT_DIR = Path(__file__).with_name("fonts")
FONT_FAMILY = "Roboto"
MONO_FAMILY = "Consolas"                # the bug report (in Windows 10 and later)
SYMBOL_FONT = "Segoe UI Symbol"         # the glyphs (in Windows 10 and later)
# The window's icon (Merger.ico, which is also the exe's): frozen, the spec puts it next to this module
ICON_FILE = Path(__file__).with_name("Merger.ico") if FROZEN else REPO_ROOT / "assets" / "Merger.ico"
LICENSES_DIR = APP_DIR / "licenses"      # (only next to the frozen exes)
WIDTH, HEIGHT = 720, 680                # the window's size at first, before Windows' scaling
MIN_WIDTH, MIN_HEIGHT = WIDTH, 520
# A row's status label is at least as wide as the widest status ("Another mod loads first"), so that a status
# changing (as while merging) doesn't move the arrows after it
STATUS_WIDTH = 140
# What runs in a thread (MergerWindow.busy) that turns off the settings and the notices' buttons
EXCLUSIVE = ("build", "remove", "update")
BAR_DELAY = 300                         # ms before a progress bar without progress shows (most scans end first)
CRITICAL_HEIGHT = 56                    # px: a critical update's notice is at least this tall
VERSION_SPACING = 24                    # px above each version in the What's new dialog but the first (half above its notes' headings)


def run(game: Path | None, stale: bool, settings: Settings | None, calls: queue.Queue, log_records: queue.Queue,
        *, updated_from: str | None = None, critical: bool = False) -> int:
    """Show the window until it closes; returns the exit code. updated_from: the version this one replaced
    (critical: by a critical update)"""
    app = make_app()
    window = MergerWindow(game, stale, settings, calls, log_records, updated_from=updated_from, critical=critical)
    sys.excepthook = window.callback_failed
    window.root.show()
    app.exec()
    return window.exit_code


def make_app() -> QApplication:
    app = QApplication(sys.argv[:1])    # (Qt reads some arguments of its own)
    app.setStyle("Fusion")
    for path in sorted(FONT_DIR.glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            log.debug(f"The font {path.name} can't be loaded")
    app.setFont(font())
    if ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(ICON_FILE)))
    apply_theme()
    QGuiApplication.styleHints().colorSchemeChanged.connect(lambda scheme: apply_theme())
    return app


def find_candidates() -> list[GameFolder]:
    """The game folders to choose from. In test mode only the test folder: game_path ignores the settings."""
    if os.environ.get(GAME_PATH_ENV):
        return [GameFolder(game_path(None), "test")]
    return find_games()


def same_folder(a: Path | None, b: Path | None) -> bool:
    return a is not None and b is not None and os.path.normcase(os.path.normpath(a)) == os.path.normcase(
        os.path.normpath(b))


def smoothui_build(installed) -> str | None:
    """The installed Smooth UI build, e.g. "Smooth UI Remastered v0.3.2, 120 fps"; None if it can't be used"""
    if installed and installed.usable:
        return f"Smooth UI Remastered {installed.version.split('-')[0]}, {installed.fps} fps"
    return None


# ---- The look ----

def colors() -> dict[str, str]:
    """theme.json's colors for the mode Windows is in (light or dark)"""
    dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    return {name: pair[dark] for name, pair in THEME.items()}


def apply_theme():
    """The palette and style sheet for the current mode (again when Windows changes it)"""
    c = colors()
    palette = QPalette(QColor(c["card"]), QColor(c["window"]))     # (button, window): the other roles from them
    for role, name in ((QPalette.WindowText, "text"), (QPalette.Text, "text"), (QPalette.ButtonText, "text"),
                       (QPalette.Base, "log"), (QPalette.AlternateBase, "card"), (QPalette.ToolTipBase, "card"),
                       (QPalette.ToolTipText, "text"), (QPalette.Highlight, "accent"),
                       (QPalette.HighlightedText, "accent_text"), (QPalette.Link, "accent"),
                       (QPalette.PlaceholderText, "muted_text")):
        palette.setColor(role, QColor(c[name]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(c["disabled_text"]))
    QApplication.setPalette(palette)
    QApplication.instance().setStyleSheet(style_sheet(c))


def style_sheet(c) -> str:
    """The window's style sheet (STYLE), and texts and the card's border colored by tone (set_tone)"""
    tones = (OK, INFO, WARNING, ERROR, UPDATE, "neutral")
    return STYLE.format(**c) + "\n".join([
        *(f'#card[tone="{tone}"] {{ border-color: {c[tone]}; }}' for tone in tones),
        *(f'QLabel[tone="{tone}"] {{ color: {c[tone]}; }}' for tone in tones),
    ]) + "\n"


# customtkinter's look: flat, rounded buttons (the main one in the accent color, the others outlined), a thin
# rounded progress bar, ring-shaped radio buttons, thin scroll bars; the card, notices and a screen's details
STYLE = """
#card {{ background: {card}; border: 2px solid {neutral}; border-radius: 6px; }}
#notice {{ background: {notice}; border-radius: 6px; }}
#notice[tone="update"] {{ background: {notice_update}; }}
#notice[tone="critical"] {{ background: {notice_critical}; }}
#notice[tone="critical"] QLabel {{ color: {notice_critical_text}; }}
#details {{ background: {card}; border-radius: 6px; }}
QLabel[tone="muted"] {{ color: {muted_text}; }}
QPushButton {{ background: transparent; color: {quiet_button_text}; border: 1px solid {quiet_button_border};
    border-radius: 6px; padding: 5px 12px; min-width: 114px; min-height: 16px; }}
QPushButton:hover {{ background: {quiet_button_hover}; }}
QPushButton:disabled {{ color: {disabled_text}; }}
QPushButton[primary="true"] {{ background: {accent}; color: {accent_text}; border: 1px solid {accent}; }}
QPushButton[primary="true"]:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton[primary="true"]:disabled {{ color: {accent_disabled_text}; }}
#notice[tone="critical"] QPushButton {{ color: {notice_critical_text}; border-color: {notice_critical_text}; }}
#notice[tone="critical"] QPushButton:hover {{ background: {notice_critical_light}; }}
#notice[tone="critical"] QPushButton[primary="true"] {{ background: {notice_critical_text}; color: {notice_critical}; }}
#notice[tone="critical"] QPushButton:disabled {{ color: {notice_critical_light}; border-color: {notice_critical_light}; }}
QProgressBar {{ background: {trough}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
QRadioButton {{ spacing: 8px; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border: 3px solid {radio_border}; border-radius: 11px; }}
QRadioButton::indicator:hover {{ border-color: {accent_hover}; }}
QRadioButton::indicator:checked {{ width: 10px; height: 10px; border: 6px solid {accent}; background: {window}; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {scrollbar}; border-radius: 4px; min-height: 30px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {scrollbar_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QPlainTextEdit, QTextBrowser {{ background: {log}; border: none; border-radius: 6px; padding: 4px; }}
QCheckBox {{ spacing: 8px; }}
"""


def font(size=13, bold=False, family=FONT_FAMILY) -> QFont:
    """A font, its size in pixels (before Windows' scaling)"""
    result = QFont(family)
    result.setPixelSize(size)
    result.setBold(bold)
    return result


def set_tone(widget, tone):
    """Color a label's text, or the card's border, by tone (or "muted"; None: the normal color)"""
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def label(text="", size=13, bold=False, tone=None, wrap=False, on_click=None, family=FONT_FAMILY) -> QLabel:
    """A label showing text as it is (not as HTML); on_click: called when it is clicked"""
    widget = QLabel(text) if on_click is None else Clickable(text, on_click)
    widget.setTextFormat(Qt.PlainText)
    widget.setFont(font(size, bold, family))
    widget.setWordWrap(wrap)
    if tone:
        set_tone(widget, tone)
    return widget


def glyph(tone=None, size=13, on_click=None) -> QLabel:
    return label(GLYPHS[tone] if tone else "", size, tone=tone, on_click=on_click, family=SYMBOL_FONT)


def button(text, command, primary=False) -> QPushButton:
    """A button; primary: drawn as the main one (a card's default button, Continue, Save)"""
    widget = QPushButton(text.replace("&", "&&"))
    widget.setProperty("primary", primary)
    widget.clicked.connect(lambda checked=False: command())
    return widget


def discard(widget):
    widget.hide()
    widget.deleteLater()


class Clickable(QLabel):
    """A label that calls on_click when clicked (a screen's row)"""
    def __init__(self, text, on_click):
        super().__init__(text)
        self.on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.on_click()


class Root(QWidget):
    """The top-level window: its close button and first paint go to the MergerWindow"""
    def __init__(self, owner: MergerWindow):
        super().__init__()
        self.owner = owner
        self.painted = False

    def closeEvent(self, event):
        if self.owner.exiting:
            event.accept()
        else:
            event.ignore()
            self.owner.close()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.painted:
            self.painted = True
            self.owner.on_shown()


class MergerWindow:
    def __init__(self, game: Path | None, stale: bool, settings: Settings | None, calls: queue.Queue,
                 log_records: queue.Queue, *, updated_from: str | None = None, critical: bool = False):
        """game: None until the user chooses it; settings: as saved (None: the first run, none yet);
        updated_from: the tools were just updated (from this version); critical: by a critical update"""
        self.game, self.stale, self.calls, self.log_records = game, stale, calls, log_records
        self.updated_from = updated_from
        self.critical_installed = critical
        self.settings = settings
        self.update_checked = False     # the update check ran (once per window)
        self.update = None              # the newer tools version it found (updates.ToolsUpdate)
        self.update_search = None       # stale mode: what the card says about it (see status_card)
        # What status_card shows (see there)
        self.found = None
        self.scan_error = None
        self.merged = None
        self.merge_error = None
        self.progress = None        # the merge's or the update's download (done, total), once known
        self.busy = None            # what runs in a thread: "scan", "build", "remove", "update"
        self.card: Card | None = None
        self.busy_since = 0.0       # when it started (time.perf_counter)
        self.card_buttons = None    # the widget holding the card's buttons, made again when they change
        self.card_buttons_shown = ()
        self.notices_shown = None
        self.pool = None
        self.reports = []           # merge_report for each merge, written to REPORT_FILE
        self.exit_code = CLOSED
        self.exiting = False
        self.closing = False        # closed while merging: no new workers (see restart_pool)
        self.polling = False
        self.scan_again = False     # the game folder changed during a scan: its result is for the old one
        self.candidates = None      # the game folders found (find_candidates), once the search ends
        self.searching = False
        self.welcome_page = self.settings_page = self.help_page = None   # made when first shown
        self.welcome_chooser = self.settings_chooser = self.update_box = None

        self.root = root = Root(self)
        root.setWindowTitle(TITLE)
        root.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.center(WIDTH, HEIGHT)
        for key in (Qt.Key_Return, Qt.Key_Enter):
            QShortcut(QKeySequence(key), root).activated.connect(self.press_default)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(0)

        header = QGridLayout()
        header.setSpacing(0)
        header.setColumnStretch(0, 1)
        header.addWidget(label(TITLE, 20, bold=True), 0, 0)
        self.header_buttons = QWidget()     # Settings and Help (only on the main page)
        row = QHBoxLayout(self.header_buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.settings_button = button(SETTINGS, self.open_settings)
        row.addWidget(self.settings_button)
        row.addWidget(button(HELP, self.open_help))
        header.addWidget(self.header_buttons, 0, 1)
        self.context = label(tone="muted")
        self.context.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)    # a long path is cut, not widening
        self.context.setContentsMargins(0, 8, 0, 8)     # (away from the buttons above it)
        header.addWidget(self.context, 1, 0, 1, 2)
        layout.addLayout(header)
        self.notices = QVBoxLayout()
        self.notices.setSpacing(0)
        layout.addLayout(self.notices)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)

        # The main page: the card, the list of screens, the log, the footer
        self.main_page = main = QWidget()
        page = QVBoxLayout(main)
        page.setContentsMargins(0, 8, 0, 0)
        page.setSpacing(8)
        self.card_frame = QFrame(objectName="card")
        page.addWidget(self.card_frame)
        card = QGridLayout(self.card_frame)
        card.setContentsMargins(16, 14, 16, 14)
        card.setHorizontalSpacing(8)
        card.setVerticalSpacing(2)
        card.setColumnStretch(1, 1)
        self.card_glyph = glyph(size=26)
        self.card_glyph.setFixedWidth(32)
        card.addWidget(self.card_glyph, 0, 0, 2, 1, Qt.AlignTop)
        self.card_headline = label(size=17, bold=True, wrap=True)
        card.addWidget(self.card_headline, 0, 1)
        self.card_text = label(wrap=True)
        card.addWidget(self.card_text, 1, 1)
        self.card_bar = QProgressBar(textVisible=False)
        self.card_bar.setFixedHeight(8)
        policy = self.card_bar.sizePolicy()
        policy.setRetainSizeWhenHidden(True)    # its space stays, so the card's height doesn't change with it
        self.card_bar.setSizePolicy(policy)
        self.card_bar.hide()
        card.addWidget(self.card_bar, 2, 1)
        self.card_grid = card

        self.screens = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        self.screens.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self.screens.setWidget(holder)
        self.list_layout = QVBoxLayout(holder)
        self.list_layout.setContentsMargins(8, 0, 8, 0)
        self.list_layout.addStretch()
        self.list_body = None       # the list's grid (show_screens makes it)
        self.list_grid = None
        page.addWidget(self.screens, 1)
        self.expanded = set()       # the screens whose details show
        self.auto_opened = set()    # the screens whose details opened by themselves (once, so closing sticks)
        self.list_content = None    # what the list shows (show_screens)
        self.rows = {}              # by path: (FileState, its cells, the grid row for its details)
        self.rows_merging = False   # whether rows show a merge's progress, not their FileState
        self.keep_order = False     # since a merge (until Re-scan): rows stay where they were (see show_screens)
        self.row_order = {}         # by path: the row's position in the list, as last shown
        self.details = {}           # by path: (its frame, its button or None)
        self.list_row = 0

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.hide()
        page.addWidget(self.log_view, 1)
        footer = QHBoxLayout()
        self.log_button = button(SHOW_LOG, self.toggle_log)
        footer.setSpacing(8)
        footer.addWidget(self.log_button)
        footer.addStretch()
        footer.addWidget(button(OPEN_LOG_FOLDER, lambda: os.startfile(gui.DATA_DIR)))
        footer.addWidget(button(WRITE_BUG_REPORT, self.open_bug_report))
        page.addLayout(footer)
        self.pages.addWidget(main)

        self.refresh()
        if settings is None:
            self.welcome_page = self.make_welcome()
            self.find_game_folders()
        self.show_page(self.welcome_page or main)
        self.poll_timer = QTimer(interval=50, timeout=self.poll)
        # What threads hand over is shown once the window shows (handling it first would delay that), or after
        # a while at the latest (e.g. started minimized)
        QTimer.singleShot(1000, self.start_polling)
        QTimer.singleShot(300, self.start_pool)     # after the window shows

    def center(self, width, height):
        """Size the window, and center it on the main screen"""
        self.root.resize(width, height)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.root.move(screen.x() + max((screen.width() - width) // 2, 0),
                       screen.y() + max((screen.height() - height) // 2, 0))

    def on_shown(self):
        """The window's first paint"""
        ms = ms_since_process_start()
        log.debug(f"The window shows, {ms:.0f} ms after the merger started" if ms is not None else "The window shows")
        if self.stale:
            self.to_front()
        QTimer.singleShot(0, self.start_polling)    # once it is drawn

    def to_front(self):
        """The launcher opened the window: the user expects the game, so it must not open behind another
        window. Windows may not give a background process the focus: on top for a moment helps."""
        self.root.raise_()
        self.root.activateWindow()
        set_topmost(int(self.root.winId()), True)
        QTimer.singleShot(300, lambda: set_topmost(int(self.root.winId()), False))

    # ---- The pages: main, welcome, settings ----

    def show_page(self, page):
        """Show a page; the header's Settings and Help buttons only show on the main page"""
        self.pages.setCurrentWidget(page)
        self.header_buttons.setVisible(page is self.main_page)

    @property
    def page(self):
        return self.pages.currentWidget()

    def make_page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(4)
        self.pages.addWidget(page)
        return page, layout

    @staticmethod
    def heading(layout, text, space=18, size=14):
        layout.addSpacing(space)
        layout.addWidget(label(text, size, bold=True))

    def page_buttons(self, layout, *buttons):
        """A page's buttons, at the bottom right (the last one rightmost)"""
        layout.addStretch()
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 2)
        row.addStretch()
        for widget in buttons:
            row.addWidget(widget)
        layout.addLayout(row)

    def make_welcome(self):
        """The first run's page: what the merger does, the game folder, the launcher. Continue saves the
        settings. In stale mode, closing the window starts the game without them (the launcher opens the
        merger again next time)."""
        page, layout = self.make_page()
        self.heading(layout, WELCOME_HEADING, 0, 17)
        layout.addWidget(label(WELCOME.format(folder=PACKAGE_FOLDER), wrap=True))
        self.heading(layout, GAME_HEADING)
        self.welcome_chooser = GameChooser(self, self.welcome_chosen)
        layout.addWidget(self.welcome_chooser.widget)
        self.heading(layout, LAUNCHER_HEADING)
        layout.addWidget(self.launcher_section(self.welcome_chooser))
        self.continue_button = button(CONTINUE, self.confirm_welcome, primary=True)
        self.continue_button.setEnabled(False)     # until a folder is chosen
        self.page_buttons(layout, self.continue_button)
        return page

    def make_settings(self):
        """The settings page: the update check and the game folder (saved with Save), the launcher"""
        page, layout = self.make_page()
        self.heading(layout, SETTINGS, 0, 17)
        layout.addSpacing(10)
        self.update_box = QCheckBox(CHECK_UPDATES.replace("&", "&&"))
        layout.addWidget(self.update_box)
        self.heading(layout, GAME_HEADING)
        self.settings_chooser = GameChooser(self, lambda folder: None)
        layout.addWidget(self.settings_chooser.widget)
        self.heading(layout, LAUNCHER_HEADING)
        layout.addWidget(self.launcher_section(self.settings_chooser))
        self.save_button = button(SAVE, self.save_settings_page, primary=True)
        self.page_buttons(layout, button(CANCEL, lambda: self.show_page(self.main_page)), self.save_button)
        return page

    def make_help(self):
        """The help page: the versions, how to set the tools up, reporting a bug, the data folder, the licenses"""
        page, layout = self.make_page()
        self.heading(layout, HELP, 0, 17)
        self.heading(layout, VERSIONS_HEADING, 10)
        layout.addWidget(label(MERGER_VERSION.format(version=gui.version())))
        self.help_smoothui = label(wrap=True)
        layout.addWidget(self.help_smoothui)
        self.heading(layout, GUIDE_HEADING)
        layout.addWidget(QLabel(f'<a href="{TOOLS_README_URL}">{GUIDE_LINK}</a>', openExternalLinks=True))
        self.heading(layout, BUG_HEADING)
        layout.addWidget(label(BUG, wrap=True))
        layout.addSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(button(WRITE_BUG_REPORT, self.open_bug_report))
        row.addWidget(QLabel(f'<a href="{REPORT_URL}">{REPORT_LINK}</a>', openExternalLinks=True))
        row.addStretch()
        layout.addLayout(row)
        self.heading(layout, DATA_HEADING)
        layout.addWidget(label(DATA.format(folder=gui.DATA_DIR), wrap=True))
        layout.addSpacing(4)
        layout.addWidget(button(OPEN_DATA, lambda: os.startfile(gui.DATA_DIR)), 0, Qt.AlignLeft)
        self.heading(layout, LICENSES_HEADING)
        layout.addWidget(label(LICENSES, wrap=True))
        layout.addSpacing(4)
        licenses = button(OPEN_LICENSES, lambda: os.startfile(LICENSES_DIR))
        licenses.setEnabled(LICENSES_DIR.is_dir())
        layout.addWidget(licenses, 0, Qt.AlignLeft)
        self.back_button = button(BACK, lambda: self.show_page(self.main_page), primary=True)
        self.page_buttons(layout, self.back_button)
        return page

    def open_help(self):
        if self.help_page is None:
            self.help_page = self.make_help()
        installed = self.found.scan.installed if self.found else None
        self.help_smoothui.setText(smoothui_build(installed) or (capitalized(installed.message) if installed
                                                                 else SMOOTHUI_NOT_CHECKED))
        self.show_page(self.help_page)

    def open_bug_report(self):
        """The short bug report in a dialog, to read, edit and copy (see bug_report.py)"""
        from .bug_report import short_report
        text = short_report(self.game, self.found, self.window_lines(), gui.DATA_DIR / LOG_FILE)
        bug_report_dialog(self.root, text, self.save_report_zip).exec()

    def window_lines(self) -> list[str]:
        """What the window shows, as text: the card, the screens' rows and the notes"""
        lines = []
        if self.card is not None:
            lines += [self.card.headline, *self.card.sentences]
        for state, _, _ in self.rows.values():
            tone, status = row_status(state)
            line = f"{screen_name(state.path)}: {status}" + (f" ({state.winner})" if state.winner else "")
            lines.append(f"{line}: {capitalized(state.message)}" if tone == ERROR else line)
        return lines + [text for text, _ in self.notes()] if self.found else lines

    def save_report_zip(self):
        """Save the full bug report (see bug_report.py), then show it in Explorer"""
        from .bug_report import save_bug_report
        try:
            path = save_bug_report(gui.DATA_DIR, self.game, mods_settings(), self.found)
        except SmoothUIError as e:
            log.error(f"The bug report can't be saved: {e}")
            show_error(self.root, BUG_REPORT_TITLE, BUG_REPORT_FAILED.format(error=e))
            return
        log.info(f"Bug report saved: {path}")
        subprocess.Popen(f'explorer /select,"{path}"')

    def launcher_section(self, chooser) -> QWidget:
        """How to start the game: the setup link, and the shortcut button (its icon: the game's, in the folder
        chosen in chooser)"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(label(LAUNCHER, wrap=True))
        link = QLabel(f'<a href="{TOOLS_README_URL}">{SETUP_LINK}</a>', openExternalLinks=True)
        layout.addWidget(link)
        message = label(wrap=True)
        message.hide()      # until there is something to say
        layout.addSpacing(4)
        layout.addWidget(button(CREATE_SHORTCUT, lambda: self.make_shortcut(chooser, message)), 0, Qt.AlignLeft)
        layout.addWidget(message)
        return widget

    def make_shortcut(self, chooser, message):
        """A desktop shortcut to the launcher, with the icon of the game in the folder chosen in chooser"""
        launcher = APP_DIR / LAUNCHER_EXE
        game = chooser.selected() or self.game
        try:
            if not launcher.is_file():
                fail(NO_LAUNCHER)
            path = create_shortcut(desktop_folder(), SHORTCUT_NAME, launcher,
                                   f"{Path(game) / GAME_EXE},0" if game else None, SHORTCUT_DESCRIPTION)
        except SmoothUIError as e:
            log.error(f"The launcher shortcut can't be made: {e}")
            message.setText(f"{capitalized(str(e))}.")
            set_tone(message, ERROR)
        else:
            log.info(f"Launcher shortcut made: {path}")
            message.setText(SHORTCUT_MADE.format(name=SHORTCUT_NAME))
            set_tone(message, OK)
        message.show()

    def find_game_folders(self):
        """Look for the game's folders, in a thread (it can take up to a second), unless that's done or running"""
        if self.candidates is None and not self.searching:
            self.searching = True
            in_thread(self.calls, find_candidates, "found_game_folders")

    def found_game_folders(self, folders, error):
        self.searching = False
        self.candidates = folders or []
        log.info(f"Game folders found: {', '.join(str(folder.path) for folder in self.candidates) or 'none'}")
        if self.page is self.welcome_page:
            self.welcome_chooser.show(self.candidates, self.welcome_chooser.selected() or self.game)
        elif self.page is self.settings_page:
            self.settings_chooser.show(self.candidates, self.settings_chooser.selected() or self.game)

    def welcome_chosen(self, folder):
        """A folder chosen on the welcome page: scanned at once, so that the result is ready on Continue"""
        self.continue_button.setEnabled(True)
        if not same_folder(folder, self.game):
            self.change_game(folder)

    def confirm_welcome(self):
        """Continue: save the settings, with the folder chosen, and show the main page"""
        folder = self.welcome_chooser.selected()
        if folder is not None and self.save_game(folder, self.welcome_chooser):
            self.show_page(self.main_page)

    def open_settings(self):
        if self.settings_page is None:
            self.settings_page = self.make_settings()
        self.settings_chooser.show(self.candidates or [], self.game, searching=self.candidates is None)
        self.update_box.setChecked(self.check_updates)
        self.find_game_folders()
        self.show_page(self.settings_page)

    def save_settings_page(self):
        """Save: the game folder chosen and the update check, if either changed (a new folder is scanned);
        back to the main page"""
        folder = self.settings_chooser.selected() or self.game
        new_folder = folder is not None and not same_folder(folder, self.game)
        if folder is not None and (new_folder or self.update_box.isChecked() != self.check_updates):
            if not self.save_game(folder, self.settings_chooser, self.update_box.isChecked()):
                return
            if new_folder:
                self.change_game(folder)
        self.show_page(self.main_page)

    def save_game(self, folder, chooser, check_updates=None) -> bool:
        """Save the settings with the game folder, and check_updates (None: as it is); a failure is said in
        chooser"""
        check_updates = self.check_updates if check_updates is None else check_updates
        settings = Settings(folder, check_updates, self.settings.last_update_check if self.settings else None)
        try:
            save_settings(settings, gui.DATA_DIR)
        except SmoothUIError as e:
            log.error(f"The settings can't be saved: {e}")
            chooser.say(SETTINGS_NOT_SAVED.format(error=e), ERROR)
            return False
        log.info(f"Settings saved: the game folder is {folder}; update check {'on' if check_updates else 'off'}")
        self.settings = settings
        return True

    @property
    def check_updates(self) -> bool:
        return self.settings.check_updates if self.settings else True

    def change_game(self, game):
        """Use another game folder: scan it, after the scan that runs now (if any), whose result is for the
        old one"""
        log.info(f"Game folder: {game}")
        self.game = game
        self.found = self.scan_error = self.merged = self.merge_error = None
        self.keep_order = False
        if self.busy == "scan":
            self.scan_again = True
            self.refresh()
        else:
            start_scan(game, self.calls)
            self.set_busy("scan")   # now, not once "scanning" arrives: another change meanwhile must wait for it

    # ---- Qt's thread: what threads hand over ----

    def start_polling(self):
        if not self.polling:
            self.polling = True
            self.poll_timer.start()
            self.poll()

    def poll(self):
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
        tone = ERROR if record.levelno >= logging.ERROR else WARNING if record.levelno >= logging.WARNING else None
        style = QTextCharFormat()
        if tone:
            style.setForeground(QColor(colors()[tone]))
        cursor = QTextCursor(self.log_view.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text + "\n", style)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def toggle_log(self):
        shown = not self.log_view.isVisible()
        self.log_view.setVisible(shown)
        self.log_button.setText(HIDE_LOG if shown else SHOW_LOG)
        if shown:
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def start_pool(self):
        # Imported here, as in close and merge: they take a while to import, and the window shows first
        from ..parallel import MAX_WORKERS, make_pool, warm_up, worker_count
        workers = worker_count(MAX_WORKERS)
        if workers > 1:
            self.pool = make_pool(workers)
            warm_up(self.pool, workers, "smoothui.patcher.patcher_core")

    # ---- What the window shows ----

    def refresh(self):
        """Show the window's state: the header's context line, the notices, the card, the screens"""
        installed = self.found.scan.installed if self.found else None
        context = [smoothui_build(installed), str(self.game) if self.game else None]
        self.context.setText("  ·  ".join(text for text in context if text))
        self.settings_button.setEnabled(self.busy not in EXCLUSIVE)  # no other game folder meanwhile
        self.show_notices(installed)
        self.show_card()
        self.show_screens()

    def show_notices(self, installed):
        """The notices, each (text, tone, buttons ((label, action), see card_actions)), made again only when
        they change. Their buttons are off while a merge, a removal or an update runs."""
        notices = []
        if names := [name for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV, UPDATE_URL_ENV) if os.environ.get(name)]:
            notices.append((TEST_MODE.format(names=" and ".join(names)), INFO, ()))
        if self.updated_from:
            notices.append((UPDATED.format(version=tools_version()), OK, ()))
        if self.update and not self.stale:
            text, tone = (CRITICAL_UPDATE, CRITICAL) if self.update.critical else (UPDATE_AVAILABLE, UPDATE)
            notices.append((text.format(version=self.update.version), tone,
                            ((WHATS_NEW, "whats_new"), (UPDATE_NOW, "update"))))
        elif self.critical_update() and not self.card_offers_update():  # (stale mode: only a critical one)
            notices.append((CRITICAL_UPDATE.format(version=self.update.version), CRITICAL,
                            ((WHATS_NEW, "whats_new"), (UPDATE_NOW, "update"))))
        if installed and installed.newer_release:
            notices.append((NEWER_RELEASE.format(version=installed.newer_release), INFO,
                            ((DOWNLOAD_PAGE, "files_page"),)))
        enabled = self.busy not in EXCLUSIVE
        if (notices, enabled) == self.notices_shown:
            return
        self.notices_shown = (notices, enabled)
        while item := self.notices.takeAt(0):
            discard(item.widget())
        actions = self.card_actions()
        for text, tone, buttons in notices:
            banner = QFrame(objectName="notice")
            set_tone(banner, tone)
            row = QHBoxLayout(banner)
            row.setContentsMargins(10, 4, 10, 4)
            row.setSpacing(6)
            critical = tone == CRITICAL     # taller, its text bold, Update now filled in (see STYLE)
            if critical:
                banner.setMinimumHeight(CRITICAL_HEIGHT)
                row.setContentsMargins(12, 8, 12, 8)
            icon = glyph(tone, 16 if critical else 14)
            icon.setFixedWidth(18 if critical else 16)  # the glyphs' widths differ: the texts start at one place
            icon.setAlignment(Qt.AlignHCenter)
            row.addWidget(icon, 0, Qt.AlignVCenter if critical else Qt.AlignTop)
            row.addWidget(label(text, bold=critical, wrap=True), 1)
            for name, action in buttons:
                widget = button(name, actions[action], primary=critical and action == "update")
                widget.setEnabled(enabled)
                row.addWidget(widget)
            wrapper = QWidget()     # the space above it
            outer = QVBoxLayout(wrapper)
            outer.setContentsMargins(0, 8, 0, 0)
            outer.addWidget(banner)
            self.notices.addWidget(wrapper)

    def show_card(self):
        """The card for the window's state (see gui_texts.status_card). Its layout doesn't change while
        something runs, so the list below doesn't move: the progress bar's space is always there, and a
        card without buttons keeps the last card's, disabled."""
        card = status_card(self.found, stale=self.stale, busy=self.busy, scan_error=self.scan_error,
                           merged=self.merged, merge_error=self.merge_error, progress=self.progress,
                           update=self.update.version if self.update else None,
                           critical=self.critical_update(), update_search=self.update_search,
                           critical_installed=self.critical_installed)
        if card == self.card:
            self.enable_buttons(True)
            return
        self.card = card
        set_tone(self.card_frame, card.tone)
        self.card_glyph.setText(GLYPHS[card.tone])
        set_tone(self.card_glyph, card.tone)
        self.card_headline.setText(card.headline)
        self.card_text.setText(" ".join(card.sentences))
        self.card_text.setVisible(bool(card.sentences))
        self.show_bar(card)

        actions = self.card_actions()
        buttons = tuple((text, action) for text, action in card.buttons if action in actions)
        if card.busy and not buttons:
            self.enable_buttons(False)
            return
        if buttons != self.card_buttons_shown:
            self.card_buttons_shown = buttons
            if self.card_buttons is not None:
                discard(self.card_buttons)
                self.card_buttons = None
            if buttons:
                self.card_buttons = row = QWidget()
                if self.stale:      # stacked, at full width, so that each always fits
                    layout = QVBoxLayout(row)
                    layout.setSpacing(6)
                    for index, (text, action) in enumerate(buttons):
                        layout.addWidget(button(text, actions[action], primary=index == 0 and card.default))
                else:               # at the right, the default last
                    layout = QHBoxLayout(row)
                    layout.setSpacing(8)
                    layout.addStretch()
                    for index, (text, action) in reversed(list(enumerate(buttons))):
                        layout.addWidget(button(text, actions[action], primary=index == 0 and card.default))
                layout.setContentsMargins(0, 4, 0, 0)
                self.card_grid.addWidget(row, 3, 0 if self.stale else 1, 1, 2 if self.stale else 1)
        self.enable_buttons(True)

    def show_bar(self, card):
        """The card's progress bar: shown while something runs; without progress, only after BAR_DELAY"""
        bar = self.card_bar
        if not card.busy:
            bar.hide()
            return
        if card.progress is None:
            bar.setRange(0, 0)      # (moving back and forth)
        else:
            bar.setRange(0, 1000)
            bar.setValue(round(card.progress * 1000))
        delay = 0 if card.progress is not None else BAR_DELAY - round((time.perf_counter() - self.busy_since) * 1000)
        if delay > 0:
            QTimer.singleShot(delay, lambda: self.card is card and bar.show())
        else:
            bar.show()

    def enable_buttons(self, on):
        """The card's buttons (on or off), and those in the screens' details (also off while something runs)"""
        for widget in self.card_buttons.findChildren(QPushButton) if self.card_buttons is not None else []:
            widget.setEnabled(on)
        for _, widget in self.details.values():
            if widget is not None:
                widget.setEnabled(on and not self.busy)

    def press_default(self):
        if self.page is self.welcome_page:
            if self.continue_button.isEnabled():
                self.continue_button.click()
            return
        if self.page is self.settings_page:
            self.save_button.click()
            return
        if self.page is self.help_page:
            self.back_button.click()
            return
        actions = self.card_actions()
        buttons = [action for _, action in self.card.buttons if action in actions] if self.card else []
        shown = self.card_buttons.findChildren(QPushButton) if self.card_buttons is not None else []
        if buttons and self.card.default and all(widget.isEnabled() for widget in shown):  # (not while re-scanning)
            actions[buttons[0]]()

    def card_actions(self):
        """What the card's buttons do, by action (see gui_texts.Card); a button whose action isn't here
        isn't shown"""
        return {
            "merge": self.merge,
            "merge_and_play": lambda: self.merge(play=True),
            "merge_anyway": self.merge_anyway,
            "merge_anyway_and_play": lambda: self.merge_anyway(play=True),
            "play": lambda: self.exit(PLAY),
            "abort": lambda: self.exit(ABORT),
            "remove": self.remove,
            "remove_and_play": lambda: self.remove(play=True),
            "rescan": self.rescan,
            "smoothui_page": lambda: webbrowser.open(SMOOTHUI_URL),
            "files_page": lambda: webbrowser.open(FILES_URL),
            "bug_report": self.open_bug_report,
            "update": self.update_now,
            "whats_new": self.whats_new,
        }

    def show_screens(self):
        """The screens other mods change, by status, mod and screen, then the line for the others, then notes:
        made again when that changes (open details stay open). After a merge, until Re-scan, the rows keep
        their places: else the next "Left out" row would move up under the mouse, as if the click had failed."""
        if self.rows_merging and self.busy:     # the scan after a merge: the rows show the merge's until it ends
            return
        states = self.found.states if self.found else []
        order = _row_order
        if self.keep_order:     # a row that wasn't shown goes after the others
            order = lambda state: (self.row_order.get(state.path, len(self.row_order)), _row_order(state))
        shown = sorted((state for state in states if state.state != State.NOTHING_TO_DO), key=order)
        self.row_order = {state.path: index for index, state in enumerate(shown)}
        others = len(states) - len(shown)
        notes = self.notes() if self.found else []
        content = ([(state, self.found.scan.files[state.path].shippers) for state in shown], others, notes)
        merging, self.rows_merging = self.rows_merging, False
        if content == self.list_content and not merging:
            return
        self.list_content = content
        if self.list_body is not None:
            discard(self.list_body)
        self.list_body = QWidget()
        self.list_grid = grid = QGridLayout(self.list_body)
        grid.setContentsMargins(0, 0, 0, 8)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(5, 1)
        grid.setColumnMinimumWidth(0, 24)
        grid.setColumnMinimumWidth(3, STATUS_WIDTH + 16)
        self.rows, self.details = {}, {}
        self.list_row = 0
        if shown:
            self.list_heading(SCREENS_HEADING)
            if any("merged" in row_status(state)[1].lower() for state in shown):
                note = label(SCREENS_NOTE, tone="muted", wrap=True)
                note.setContentsMargins(0, 0, 0, 4)
                self.list_grid.addWidget(note, self.list_row, 0, 1, 6)
                self.list_row += 1
        for state in shown:
            self.screen_row(state)
        if others and shown:
            self.list_line(unchanged_screens(others))
        if notes:
            self.list_heading(NOTES_HEADING)
            for text, tone in notes:
                self.list_line(text, tone)
        self.list_layout.insertWidget(0, self.list_body)

    def list_heading(self, text):
        heading = label(text, 14, bold=True)
        heading.setContentsMargins(0, 10 if self.list_row else 0, 0, 4)
        self.list_grid.addWidget(heading, self.list_row, 0, 1, 6)
        self.list_row += 1

    def list_line(self, text, tone=None):
        self.list_grid.addWidget(glyph(tone), self.list_row, 0, Qt.AlignTop)
        line = label(text, wrap=True)
        line.setContentsMargins(0, 4, 0, 0)
        self.list_grid.addWidget(line, self.list_row, 1, 1, 5)
        self.list_row += 1

    def screen_row(self, state: FileState):
        """A screen's row: its glyph, name, mod and status; a click shows its details, in the grid row below
        it, kept for them"""
        tone, status = row_status(state)
        toggle = lambda: self.toggle_row(state.path)
        cells = [glyph(tone, on_click=toggle), label(screen_name(state.path), on_click=toggle),
                 label(state.winner or state.previous or "", tone="muted", on_click=toggle), label(status, on_click=toggle),
                 label("▸", 16, tone="muted", on_click=toggle, family=SYMBOL_FONT)]
        cells[0].setFixedWidth(24)
        for column, cell in enumerate(cells):
            cell.setContentsMargins(0, 1, 0 if column in (0, 4) else 16, 1)
            self.list_grid.addWidget(cell, self.list_row, column)
        self.rows[state.path] = (state, cells, self.list_row + 1)
        self.list_row += 2
        if (tone == ERROR or state.fixes) and state.path not in self.auto_opened:
            self.auto_opened.add(state.path)    # the status alone doesn't say what went wrong
            self.expanded.add(state.path)
        if state.path in self.expanded:
            self.show_details(state.path)

    def show_row_status(self, path, tone, status):
        cells = self.rows[path][1]
        cells[0].setText(GLYPHS[tone])
        set_tone(cells[0], tone)
        cells[3].setText(status)

    def toggle_row(self, path):
        """Show or hide a screen's details"""
        self.expanded ^= {path}
        if path in self.expanded:
            self.show_details(path)
        elif path in self.details:
            discard(self.details.pop(path)[0])
            self.rows[path][1][4].setText("▸")

    def show_details(self, path):
        """Below a screen's row: its file name, the full message, the other mods that change it, and Patch
        anyway (a screen left out for its rescale's problems, never for a failure) or its revert"""
        state, cells, row = self.rows[path]
        cells[4].setText("▾")
        frame = QFrame(objectName="details")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 6, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(label(screen_label(path), tone="muted", wrap=True))
        layout.addWidget(label(capitalized(state.message) + (":" if state.fixes else ""), wrap=True))
        for name in state.fixes:
            layout.addWidget(label(f"•  {fix_text(name)}", wrap=True))
        if also := also_changed(self.found.scan.files[path], state.winner):
            layout.addWidget(label(also, wrap=True))
        widget = None
        if state.state == State.NOT_HELD and state.cause == "left out":
            text, note = PATCH_ANYWAY, PATCH_ANYWAY_NOTE
            command = lambda: self.merge(self.stale, patch_anyway=[path])
        elif state.state == State.UP_TO_DATE and state.cause == "forced":
            text, note = REVERT, REVERT_NOTE.format(mod=state.winner)
            command = lambda: self.merge(self.stale, revert=path)
        else:
            text = None
        if text:
            buttons = QHBoxLayout()
            buttons.setContentsMargins(0, 4, 0, 0)
            buttons.setSpacing(10)
            widget = button(text, command)
            widget.setEnabled(not self.busy)
            buttons.addWidget(widget)
            buttons.addWidget(label(note, wrap=True), 1)
            layout.addLayout(buttons)
        holder = QWidget()      # the space around it
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 2, 16, 8)
        outer.addWidget(frame)
        self.details[path] = (holder, widget)
        self.list_grid.addWidget(holder, row, 1, 1, 5)

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
            held = {state.path for state in self.found.states if state.state == State.UP_TO_DATE}
            notes += [(COMPAT_REMOVABLE.format(folder=folder), INFO) for folder in result.removable_compat(held)]
        return [(capitalized(text), tone) for text, tone in notes]

    # ---- The scan ----

    def set_busy(self, what):
        """What runs in a thread now ("scan", "build", "remove", "update"; None: nothing)"""
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
        QTimer.singleShot(BAR_DELAY, lambda: self.busy == "scan" and self.refresh())

    def scanned(self, result, error):
        self.set_busy(None)
        if self.scan_again:     # the game folder changed meanwhile (change_game)
            self.scan_again = False
            start_scan(self.game, self.calls)
            self.set_busy("scan")
            return
        if error is not None:
            self.found, self.scan_error = None, error_text(error)
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
        self.look_for_updates()

    def rescan(self, keep_result=False):
        """Scan again; the last merge's result (and the rows' places) stays only with keep_result (right after
        that merge)"""
        if not keep_result:
            self.merged = self.merge_error = None
            self.keep_order = False
        start_scan(self.game, self.calls)

    # ---- Updates ----

    def look_for_updates(self):
        """After a scan: the update check (once), unless the settings turn it off, and not while something
        runs. In stale mode nothing waits for it, except for an unknown Smooth UI build: the card then says
        it is looking (a newer version of the tools may know the build)."""
        if self.update_checked or not self.check_updates or self.busy or (self.found is None and self.scan_error is None):
            return
        self.update_checked = True
        waited_for = self.card_offers_update()
        if waited_for:
            self.update_search = LOOKING
            self.refresh()
        last_check = self.settings.last_update_check if self.settings else None
        in_thread(self.calls, gui.find_updates, "updates_checked", waited_for, last_check)

    def card_offers_update(self) -> bool:
        """Stale mode, an unknown Smooth UI build: the card offers a newer version (see status_card), and
        no notice does"""
        return self.stale and self.found is not None and self.found.scan.installed.status == Status.UNKNOWN

    def critical_update(self) -> bool:
        return self.update is not None and self.update.critical

    def updates_checked(self, found, error):
        """The update check's result (updates.Found; None if it failed or was skipped, which shows nothing).
        New manifests (or corrected ones) may change what is known about the installed Smooth UI: then it scans
        again."""
        looking, self.update_search = self.update_search == LOOKING, None
        if found is not None:
            self.save_check_time()
            self.update = found.update
            if looking and found.update is None:
                self.update_search = NOTHING_FOUND
            if found.downloaded and self.found is not None:
                log.info("Scanning again, with the new manifests")
                if self.busy == "scan":
                    self.scan_again = True
                elif not self.busy:
                    self.rescan()
        self.refresh()

    def save_check_time(self):
        """Keep the time of the update check just made in the settings (see updates.RECENT)"""
        if self.settings is None:
            return      # none saved yet: the game folder came from the test variable
        settings = replace(self.settings, last_update_check=int(time.time()))
        try:
            save_settings(settings, gui.DATA_DIR)
        except SmoothUIError as e:
            log.warning(f"The update check's time can't be saved: {e}")
            return
        self.settings = settings

    def whats_new(self):
        if whats_new_dialog(self.root, self.update).exec() == QDialog.Accepted:
            self.update_now()

    def update_now(self):
        """Update now (the notice, What's new, stale mode's card): download and unpack the new version in a
        thread, then start it as the helper (self_update.py) and exit"""
        update = self.update
        if update is None or self.busy:
            return
        log.info(f"Updating to Smooth UI Tools v{update.version}")
        self.set_busy("update")
        self.progress = None
        self.refresh()

        def progressed(done, total):
            self.calls.put(("update_progress", (done, total)))

        def prepared(root, error):
            self.set_busy(None)
            self.progress = None
            if error is None:
                try:
                    self_update.start_helper(root, gui.APP_DIR, tools_version(), self.pids(), update.critical)
                except SmoothUIError as e:
                    error = e
            if error is not None:
                log.error(f"The update failed: {error_text(error)}")
                self.refresh()
                show_error(self.root, UPDATE_FAILED_TITLE,
                           UPDATE_FAILED.format(version=update.version, error=error_text(error)))
                return
            self.exit(self_update.exit_code(self.stale))

        in_thread(self.calls, self_update.prepare, prepared, update, gui.DATA_DIR, progressed)

    def pids(self):
        """The processes the helper waits for: this one, and the launcher that started it (stale mode)"""
        return [os.getpid(), os.getppid()] if self.stale else [os.getpid()]

    def update_progress(self, done, total):
        if self.busy == "update":
            self.progress = (done, total)
            self.show_card()

    # ---- Merge, remove ----

    def merge_anyway(self, play=False):
        """Merge every screen that was left out anyway (the card's button), once the user confirms"""
        paths = left_out(self.found, self.merged)
        screens = "\n".join(f"•  {screen_label(path)}" for path in paths)
        if paths and ask_yes_no(self.root, PATCH_ANYWAY_TITLE, PATCH_ANYWAY_QUESTION.format(screens=screens),
                                warning=True):
            self.merge(play, patch_anyway=paths)

    def merge(self, play=False, patch_anyway=(), revert=None):
        """Build the package in a thread, then scan again; with play (stale mode), a clean build plays at once,
        unless a critical update is known by then (it fixes a bug that may have made this build wrong): the
        window stays open, with its notice. patch_anyway: files to force too; revert: a forced file not to
        force (see build)"""
        self.set_busy("build")
        self.merged = self.merge_error = self.progress = None
        self.keep_order = True
        self.refresh()
        for path in self.rows:      # each says "Done" when its file is (see merged_file)
            if self.found.scan.files[path].winner:
                self.show_row_status(path, *merging_status(None))
                self.rows_merging = True
        from ..log import HEADING
        log.info("Merging", extra=HEADING)

        def finished(result, error):
            self.set_busy(None)
            self.progress = None
            if error is not None:
                self.merge_error = error_text(error)
                log.error(f"Merging failed: {self.merge_error}")
            else:
                self.merged = result
                if play and result.clean:
                    if not self.critical_update():
                        self.exit(PLAY)
                        return
                    log.warning("Merged, but not playing yet: a critical update of Smooth UI Tools is available")
            self.rescan(keep_result=True)
        in_thread(self.calls, self.build, finished, play, patch_anyway, revert)

    def build(self, play=False, patch_anyway=(), revert=None):
        """Build the package, forcing what the last build forced (the same copies), and patch_anyway, but
        not revert. Its progress goes to the window as it goes (merged_file, progressed). Then new workers
        for the next merge, unless the game starts now (play, the build is clean, and no critical update is
        known; one found just after this keeps the old workers, which still work)."""
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
            log.info(f"Patching anyway, as you chose: {', '.join(screen_label(path) for path in patch_anyway)}")
            force += [path for path in patch_anyway if path not in force]
        if revert in force:
            log.info(f"Reverting {screen_label(revert)} to the unpatched screen, as you chose")
            force.remove(revert)
        result = None
        try:
            result = build_package(self.game, settings, force=force, pool=self.pool, data_dir=gui.DATA_DIR,
                                   on_file=on_file, progress=lambda *progress: self.calls.put(("progressed", progress)))
        finally:
            if not (play and result is not None and result.clean and not self.critical_update()):
                self.restart_pool()
        self.reports.append(merge_report(result, started))
        try:
            write_file(gui.DATA_DIR / REPORT_FILE, json.dumps(self.reports, indent=1).encode("utf-8"))
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
        if not play and not ask_yes_no(self.root, REMOVE_TITLE, REMOVE_QUESTION):
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
                log.error(f"The package can't be removed: {error_text(error)}")
                show_error(self.root, REMOVE_TITLE, REMOVE_FAILED.format(error=error_text(error)))
            elif play:
                self.exit(PLAY)
                return
            self.rescan()
        in_thread(self.calls, work, finished)

    # ---- Closing ----

    def close(self):
        """The window's close button"""
        if self.busy == "build":
            if not ask_yes_no(self.root, STOP_MERGE_TITLE, STOP_MERGE[self.stale], warning=True):
                return
            log.warning("Closed while merging: deleting the package" + ("; the game starts" if self.stale else ""))
            from ..parallel import kill_workers
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
        self.exiting = True
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.hide()
        QApplication.exit(code)

    def callback_failed(self, kind, error, tb):
        """sys.excepthook: an exception in a slot (Qt would only print it)"""
        log.error("".join(traceback.format_exception(kind, error, tb)).rstrip())
        msg_box(FAILED.format(error=f"{kind.__name__}: {error}", log=LOG_FILE), MB_ICONERROR)


class GameChooser:
    """The game folder chooser on the welcome and settings pages: a radio button per folder, then Choose
    another folder..., then a line saying what's going on, or why a chosen folder isn't the game's.
    on_change(folder) is called whenever a folder is selected (by the user, or by show)."""

    def __init__(self, window: MergerWindow, on_change):
        self.window, self.on_change = window, on_change
        self.folders: list[GameFolder] = []
        self.radios = []
        self.widget = QWidget()
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.rows = None    # the folders' grid (show makes it)
        self.layout = layout
        layout.addWidget(button(CHOOSE_FOLDER, self.ask), 0, Qt.AlignLeft)
        self.message = label(wrap=True)
        layout.addWidget(self.message)
        self.say(LOOKING_FOR_GAME)

    def show(self, folders: list[GameFolder], selected: Path | None = None, searching=False):
        """Offer folders (selected first, if it isn't one of them), selecting selected, else the first one;
        searching: the search for folders isn't done yet. Only game folders: e.g. the folder of the settings
        or the test variable may not be one (any more)."""
        folders = [folder for folder in folders if is_game_folder(folder.path)]
        if selected is not None and not is_game_folder(selected):
            selected = None
        if selected is not None and not any(same_folder(folder.path, selected) for folder in folders):
            folders = [GameFolder(selected, None), *folders]
        if self.rows is not None:
            discard(self.rows)
        self.folders, self.radios = list(folders), []
        self.rows = QWidget()
        grid = QGridLayout(self.rows)
        grid.setContentsMargins(0, 0, 0, 2)
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(2, 1)
        for index, folder in enumerate(self.folders):
            radio = QRadioButton(str(folder.path).replace("&", "&&"))
            radio.clicked.connect(lambda checked=False: self.chosen())
            grid.addWidget(radio, index, 0)
            grid.addWidget(label(folder.source or "", tone="muted"), index, 1)
            self.radios.append(radio)
        self.layout.insertWidget(0, self.rows)
        index = next((index for index, folder in enumerate(self.folders) if same_folder(folder.path, selected)),
                     0 if self.folders else None)
        if index is not None:
            self.radios[index].setChecked(True)
        self.say(LOOKING_FOR_GAME if searching else "" if self.folders else GAME_NOT_FOUND)
        if index is not None:
            self.chosen()

    def selected(self) -> Path | None:
        return next((folder.path for folder, radio in zip(self.folders, self.radios) if radio.isChecked()), None)

    def chosen(self):
        self.on_change(self.selected())

    def say(self, text, tone=None):
        self.message.setText(text)
        set_tone(self.message, tone or "muted")
        self.message.setVisible(bool(text))

    def ask(self):
        """Choose another folder...: the game folder, or a folder inside it, is offered and selected"""
        chosen = ask_folder(self.window.root, CHOOSE_FOLDER_TITLE, str(self.selected() or ""))
        if not chosen:
            return
        try:
            folder = game_folder_of(Path(chosen))
        except SmoothUIError as e:
            self.say(f"{capitalized(str(e))}.", ERROR)
            return
        log.info(f"Game folder chosen: {folder}")
        self.show(self.folders, folder)


def fix_text(name):
    """What a missing fix means, e.g. "Game bug fix missing: in Gwent, the falling rain stops (gwent-rain-loop-fix)";
    only its name for a fix this merger doesn't know"""
    try:
        return f"{fix_kind(name).header} {fix_consequence(name)} ({name})"
    except ValueError:
        return name


def _row_order(state: FileState):
    """By status (errors, then warnings, then the rest), then by mod (a screen no other mod changes now, e.g.
    its mod was removed, first), then by screen"""
    rank = {ERROR: 0, WARNING: 1, INFO: 2, OK: 3}[row_status(state)[0]]
    return rank, (state.winner or "").lower(), screen_name(state.path).lower()


def ask_folder(parent, title, initial) -> str:
    """A folder the user picks ("" if none)"""
    return QFileDialog.getExistingDirectory(parent, title, initial)


def bug_report_dialog(parent, text, save_zip) -> QDialog:
    """The bug report's text, to read, edit and copy; save_zip: called by its second button"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(BUG_REPORT_TITLE)
    dialog.resize(640, 560)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(10)
    layout.addWidget(label(BUG_DIALOG, wrap=True))
    edit = QPlainTextEdit(text)
    edit.setFont(font(12, family=MONO_FAMILY))
    layout.addWidget(edit, 1)
    copied = label(tone="muted")
    link = QLabel(f'<a href="{REPORT_URL}">{REPORT_LINK}</a>', openExternalLinks=True)

    def copy():
        QApplication.clipboard().setText(edit.toPlainText())
        copied.setText(REPORT_COPIED)
    row = QHBoxLayout()
    row.setSpacing(16)
    row.addWidget(button(COPY_REPORT, copy, primary=True))
    row.addWidget(copied)
    row.addStretch()
    row.addWidget(link)
    layout.addLayout(row)
    layout.addWidget(label(ZIP_NOTE, wrap=True, tone="muted"))
    zip_row = QHBoxLayout()
    zip_row.addWidget(button(SAVE_ZIP, save_zip))
    zip_row.addStretch()
    zip_row.addWidget(button(CLOSE, dialog.accept))
    layout.addLayout(zip_row)
    return dialog


def whats_new_dialog(parent, update) -> QDialog:
    """The release notes of each version newer than this one (update: updates.ToolsUpdate); Update now
    accepts it, Later rejects it"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(WHATS_NEW_TITLE.format(version=update.version))
    dialog.resize(600, 520)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(10)
    notes = QTextBrowser(openExternalLinks=True)
    notes.setMarkdown(release_notes(update.notes))
    # Space above each heading but the first (Markdown has no way to say it): a version's, and half as much
    # above the headings in its notes (e.g. New, Fixed)
    block = notes.document().begin().next()
    while block.isValid():
        if level := block.blockFormat().headingLevel():
            spaced = QTextBlockFormat()
            spaced.setTopMargin(VERSION_SPACING if level == 1 else VERSION_SPACING // 2)
            QTextCursor(block).mergeBlockFormat(spaced)
        block = block.next()
    layout.addWidget(notes, 1)
    layout.addWidget(label(UPDATE_NOTE, wrap=True, tone="muted"))
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addStretch()
    row.addWidget(button(LATER, dialog.reject))
    row.addWidget(button(UPDATE_NOW, dialog.accept, primary=True))
    layout.addLayout(row)
    return dialog


def show_error(parent, title, text):
    QMessageBox.critical(parent, title, text)


def ask_yes_no(parent, title, text, warning=False) -> bool:
    """A question, No the default"""
    box = QMessageBox(QMessageBox.Warning if warning else QMessageBox.Question, title, text,
                      QMessageBox.Yes | QMessageBox.No, parent)
    box.setDefaultButton(QMessageBox.No)
    return box.exec() == QMessageBox.Yes

