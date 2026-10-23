"""The merger's window (started by gui.py): whether the merged patches are up to date and what to do (the
status card), the screens other mods change and notes below it, and the log when asked for. Qt (PySide6,
Qt Widgets), imported only once the scan has started (see gui.py).

Merge builds the package in a thread (gui.in_thread), in a process pool started after the window shows (and
again after each merge); the card counts the screens done, and each screen's row says when it's done. What
the card shows comes from gui_texts.status_card, with the window's other texts.

Its pages share the header and notices: the main page (the card, the list, the log), the welcome page on the
first run (no settings yet: data\\settings.json), and the settings page. Both of the latter have the game folder
chooser (the folders find_games finds, or one the user picks) and a button that makes a launcher shortcut on
the desktop. On the first run, the scan starts on the folder the welcome preselects, so that its result is
ready when the user presses Continue, which saves the settings.

The look: Qt's Fusion style (the same on Windows 10 and 11, and it does dark mode), with the colors in
theme.json (each a light and a dark mode color, following Windows' setting) and Roboto, which ships in fonts\\.
With ROUNDED, a style sheet also draws buttons, the progress bar, radio buttons and scroll bars as rounded,
flat shapes (ROUNDED_STYLE); without it, Fusion draws them.

With --stale, the launcher opened it before starting the game. The window is the same, but it comes to the
front, the card offers Merge and play, Play without merging (worded by what that costs, state.Severity) and
Don't start the game, and a clean merge exits at once (play). Closing the window means play; while a merge
runs, only after a warning: the package, which may be half written, is then deleted, and the game starts
without it.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import sys
import time
import traceback
import webbrowser
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
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
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
    QVBoxLayout,
    QWidget,
)

from ..common import SmoothUIError, fail
from ..files import stop_writes, write_file
from ..launcher import ABORT, CLOSED, LAUNCHER_EXE, PLAY
from ..log import HEADING
from ..parallel import MAX_WORKERS, kill_workers, make_pool, warm_up, worker_count
from ..paths import APP_DIR
from ..windows import MB_ICONERROR, ms_since_process_start, msg_box
from . import SMOOTHUI_URL, TOOLS_README_URL, gui
from .game import (
    GAME_EXE,
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    GameFolder,
    Settings,
    find_games,
    game_folder_of,
    game_path,
    mods_settings,
    save_settings,
)
from .gui import LOG_FILE, REPORT_FILE, LogFormatter, error_text, in_thread, start_scan
from .gui_texts import (
    CANCEL,
    CHOOSE_FOLDER,
    CHOOSE_FOLDER_TITLE,
    COMPAT_PATCH,
    CONTINUE,
    CREATE_SHORTCUT,
    DATA,
    DATA_HEADING,
    DELETE_FAILED,
    ERROR,
    FAILED,
    GAME_HEADING,
    GAME_NOT_FOUND,
    GLYPHS,
    HIDE_LOG,
    INFO,
    LAUNCHER,
    LAUNCHER_HEADING,
    LOOKING_FOR_GAME,
    NEWER_RELEASE,
    NO_LAUNCHER,
    NOTES_HEADING,
    OK,
    OPEN_DATA,
    PATCH_ANYWAY,
    PATCH_ANYWAY_NOTE,
    PLAY_WITHOUT_SETUP,
    REMOVE_FAILED,
    REMOVE_QUESTION,
    REMOVE_TITLE,
    REVERT,
    REVERT_NOTE,
    SAVE,
    SCREENS_HEADING,
    SETTINGS,
    SETTINGS_NOT_SAVED,
    SETUP_LINK,
    SHORTCUT_DESCRIPTION,
    SHORTCUT_MADE,
    SHORTCUT_NAME,
    SHOW_LOG,
    STOP_MERGE,
    STOP_MERGE_TITLE,
    TEST_MODE,
    TITLE,
    UNKNOWN_FILES,
    WARNING,
    WELCOME,
    WELCOME_HEADING,
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
from .shortcut import create_shortcut, desktop_folder
from .state import FileState, State, carried_force

log = logging.getLogger(__name__)

THEME = json.loads(Path(__file__).with_name("theme.json").read_text(encoding="utf-8"))
FONT_DIR = Path(__file__).with_name("fonts")
FONT_FAMILY = "Roboto"
SYMBOL_FONT = "Segoe UI Symbol"         # the glyphs (in Windows 10 and later)
ICON_FILE = APP_DIR / "smoothui.ico"     # optional: Qt's own icon without it
ROUNDED = True                          # buttons, progress bar, ... drawn by ROUNDED_STYLE, not by Fusion
WIDTH, HEIGHT = 920, 680                # the window's size at first, before Windows' scaling
MIN_WIDTH, MIN_HEIGHT = 720, 520
# A row's status label is at least as wide as the widest status ("Another mod loads first"), so that a status
# changing (as while merging) doesn't move the arrows after it
STATUS_WIDTH = 140
BAR_DELAY = 300                         # ms before a progress bar without progress shows (most scans end first)


def run(game: Path | None, stale: bool, first_run: bool, calls: queue.Queue, log_records: queue.Queue) -> int:
    """Show the window until it closes; returns the exit code"""
    app = make_app()
    window = MergerWindow(game, stale, first_run, calls, log_records)
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
    QApplication.instance().setStyleSheet(style_sheet(c) + (ROUNDED_STYLE.format(**c) if ROUNDED else ""))


def style_sheet(c) -> str:
    """The card, notices and details, texts colored by tone (set_tone), and the main button"""
    tones = (OK, INFO, WARNING, ERROR, "neutral")
    return "\n".join([
        f"#card {{ background: {c['card']}; border: 2px solid {c['neutral']}; border-radius: 6px; }}",
        *(f'#card[tone="{tone}"] {{ border-color: {c[tone]}; }}' for tone in tones),
        f"#notice {{ background: {c['notice']}; border-radius: 6px; }}",
        f"#details {{ background: {c['card']}; border-radius: 6px; }}",
        *(f'QLabel[tone="{tone}"] {{ color: {c[tone]}; }}' for tone in tones),
        f'QLabel[tone="muted"] {{ color: {c["muted_text"]}; }}',
        (f'QPushButton[primary="true"] {{ background: {c["accent"]}; color: {c["accent_text"]}; '
         f'border: 1px solid {c["accent_hover"]}; border-radius: 3px; padding: 4px 14px; }}'),
        f'QPushButton[primary="true"]:hover {{ background: {c["accent_hover"]}; }}',
        f'QPushButton[primary="true"]:disabled {{ color: {c["accent_disabled_text"]}; }}',
    ]) + "\n"


# customtkinter's look: flat, rounded buttons (the main one in the accent color, the others outlined), a thin
# rounded progress bar, ring-shaped radio buttons, thin scroll bars. Without it (ROUNDED), Fusion draws them.
ROUNDED_STYLE = """
QPushButton {{ background: transparent; color: {quiet_button_text}; border: 1px solid {quiet_button_border};
    border-radius: 6px; padding: 5px 12px; min-width: 114px; min-height: 16px; }}
QPushButton:hover {{ background: {quiet_button_hover}; }}
QPushButton:disabled {{ color: {disabled_text}; }}
QPushButton[primary="true"] {{ background: {accent}; color: {accent_text}; border: 1px solid {accent};
    border-radius: 6px; padding: 5px 12px; }}
QPushButton[primary="true"]:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton[primary="true"]:disabled {{ color: {accent_disabled_text}; }}
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
QPlainTextEdit {{ background: {log}; border: none; border-radius: 6px; padding: 4px; }}
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
    def __init__(self, game: Path | None, stale: bool, first_run: bool, calls: queue.Queue, log_records: queue.Queue):
        """game: None until the user chooses it (first_run: no settings yet)"""
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
        self.welcome_page = self.settings_page = None   # made when first shown
        self.welcome_chooser = self.settings_chooser = None

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
        self.settings_button = button(SETTINGS, self.open_settings)
        header.addWidget(self.settings_button, 0, 1)
        self.context = label(tone="muted")
        self.context.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)    # a long path is cut, not widening
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
        self.list_content = None    # what the list shows (show_screens)
        self.rows = {}              # by path: (FileState, its cells, the grid row for its details)
        self.rows_merging = False   # whether rows show a merge's progress, not their FileState
        self.details = {}           # by path: (its frame, its button or None)
        self.list_row = 0

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.hide()
        page.addWidget(self.log_view, 1)
        footer = QHBoxLayout()
        self.log_button = button(SHOW_LOG, self.toggle_log)
        footer.addWidget(self.log_button)
        footer.addStretch()
        page.addLayout(footer)
        self.pages.addWidget(main)

        self.refresh()
        if first_run:
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
        _topmost(int(self.root.winId()), True)
        QTimer.singleShot(300, lambda: _topmost(int(self.root.winId()), False))

    # ---- The pages: main, welcome, settings ----

    def show_page(self, page):
        """Show a page; the header's Settings button only shows on the main page"""
        self.pages.setCurrentWidget(page)
        self.settings_button.setVisible(page is self.main_page)

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
        settings; in stale mode, the game can also start without them (the launcher opens the merger again
        next time)."""
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
        extra = [button(PLAY_WITHOUT_SETUP, lambda: self.exit(PLAY))] if self.stale else []
        self.page_buttons(layout, *extra, self.continue_button)
        return page

    def make_settings(self):
        """The settings page: the game folder (saved with Save), the launcher, the data folder"""
        page, layout = self.make_page()
        self.heading(layout, SETTINGS, 0, 17)
        self.heading(layout, GAME_HEADING, 10)
        self.settings_chooser = GameChooser(self, lambda folder: None)
        layout.addWidget(self.settings_chooser.widget)
        self.heading(layout, LAUNCHER_HEADING)
        layout.addWidget(self.launcher_section(self.settings_chooser))
        self.heading(layout, DATA_HEADING)
        layout.addWidget(label(DATA.format(folder=gui.DATA_DIR), wrap=True))
        layout.addSpacing(4)
        layout.addWidget(button(OPEN_DATA, lambda: os.startfile(gui.DATA_DIR)), 0, Qt.AlignLeft)
        self.save_button = button(SAVE, self.save_settings_page, primary=True)
        self.page_buttons(layout, button(CANCEL, lambda: self.show_page(self.main_page)), self.save_button)
        return page

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
        self.find_game_folders()
        self.show_page(self.settings_page)

    def save_settings_page(self):
        """Save: the game folder chosen, if it changed (then it is scanned); back to the main page"""
        folder = self.settings_chooser.selected()
        if folder is not None and not same_folder(folder, self.game):
            if not self.save_game(folder, self.settings_chooser):
                return
            self.change_game(folder)
        self.show_page(self.main_page)

    def save_game(self, folder, chooser) -> bool:
        """Save the settings with the game folder; a failure is said in chooser"""
        try:
            save_settings(Settings(folder), gui.DATA_DIR)
        except SmoothUIError as e:
            log.error(f"The settings can't be saved: {e}")
            chooser.say(SETTINGS_NOT_SAVED.format(error=e), ERROR)
            return False
        log.info(f"Settings saved: the game folder is {folder}")
        return True

    def change_game(self, game):
        """Use another game folder: scan it, after the scan that runs now (if any), whose result is for the
        old one"""
        log.info(f"Game folder: {game}")
        self.game = game
        self.found = self.scan_error = self.merged = self.merge_error = None
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
        workers = worker_count(MAX_WORKERS)
        if workers > 1:
            self.pool = make_pool(workers)
            warm_up(self.pool, workers, "smoothui.patcher.patcher_core")

    # ---- What the window shows ----

    def refresh(self):
        """Show the window's state: the header's context line, the notices, the card, the screens"""
        installed = self.found.scan.installed if self.found else None
        context = [str(self.game)] if self.game else []
        if installed and installed.usable:
            context.insert(0, f"Smooth UI Remastered {installed.version.split('-')[0]}, {installed.fps} fps")
        self.context.setText("  ·  ".join(context))
        self.settings_button.setEnabled(self.busy not in ("build", "remove"))  # no other game folder meanwhile
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
        while item := self.notices.takeAt(0):
            discard(item.widget())
        for text in notices:
            banner = QFrame(objectName="notice")
            row = QHBoxLayout(banner)
            row.setContentsMargins(10, 4, 10, 4)
            row.setSpacing(6)
            row.addWidget(glyph(INFO, 14))
            row.addWidget(label(text, wrap=True), 1)
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
                           merged=self.merged, merge_error=self.merge_error, progress=self.progress)
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
                        layout.addWidget(button(text, actions[action], primary=index == 0))
                else:               # at the right, the default last
                    layout = QHBoxLayout(row)
                    layout.setSpacing(8)
                    layout.addStretch()
                    for index, (text, action) in reversed(list(enumerate(buttons))):
                        layout.addWidget(button(text, actions[action], primary=index == 0))
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
        actions = self.card_actions()
        buttons = [action for _, action in self.card.buttons if action in actions] if self.card else []
        shown = self.card_buttons.findChildren(QPushButton) if self.card_buttons is not None else []
        if buttons and all(widget.isEnabled() for widget in shown):     # (not while a re-scan has them disabled)
            actions[buttons[0]]()

    def card_actions(self):
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
        """The screens other mods change, by status, mod and screen, then the line for the others, then notes:
        made again when that changes (open details stay open)"""
        if self.rows_merging and self.busy:     # the scan after a merge: the rows show the merge's until it ends
            return
        states = self.found.states if self.found else []
        shown = sorted((state for state in states if state.state != State.NOTHING_TO_DO), key=_row_order)
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
        grid.setColumnMinimumWidth(3, STATUS_WIDTH + 16)
        self.rows, self.details = {}, {}
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
                 label(state.winner or "", tone="muted", on_click=toggle), label(status, on_click=toggle),
                 label("▸", 16, tone="muted", on_click=toggle, family=SYMBOL_FONT)]
        cells[0].setFixedWidth(24)
        for column, cell in enumerate(cells):
            cell.setContentsMargins(0, 1, 0 if column in (0, 4) else 16, 1)
            self.list_grid.addWidget(cell, self.list_row, column)
        self.rows[state.path] = (state, cells, self.list_row + 1)
        self.list_row += 2
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
        layout.addWidget(label(capitalized(state.message), wrap=True))
        if also := also_changed(self.found.scan.files[path], state.winner):
            layout.addWidget(label(also, wrap=True))
        widget = None
        if state.state == State.NOT_HELD and state.cause == "left out":
            text, note = PATCH_ANYWAY, PATCH_ANYWAY_NOTE
            command = lambda: self.merge(self.stale, patch_anyway=path)
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
                self.merge_error = error_text(error)
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
            result = build_package(self.game, settings, force=force, pool=self.pool, data_dir=gui.DATA_DIR,
                                   on_file=on_file, progress=lambda *progress: self.calls.put(("progressed", progress)))
        finally:
            if not (play and result is not None and result.clean):
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
        searching: the search for folders isn't done yet"""
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


def _row_order(state: FileState):
    """By status (errors, then warnings, then the rest), then by mod (a screen no other mod changes now, e.g.
    its mod was removed, first), then by screen"""
    rank = {ERROR: 0, WARNING: 1, INFO: 2, OK: 3}[row_status(state)[0]]
    return rank, (state.winner or "").lower(), screen_name(state.path).lower()


def ask_folder(parent, title, initial) -> str:
    """A folder the user picks ("" if none)"""
    return QFileDialog.getExistingDirectory(parent, title, initial)


def show_error(parent, title, text):
    QMessageBox.critical(parent, title, text)


def ask_yes_no(parent, title, text, warning=False) -> bool:
    """A question, No the default"""
    box = QMessageBox(QMessageBox.Warning if warning else QMessageBox.Question, title, text,
                      QMessageBox.Yes | QMessageBox.No, parent)
    box.setDefaultButton(QMessageBox.No)
    return box.exec() == QMessageBox.Yes


def _topmost(hwnd: int, on: bool):
    """Keep a window above all others, or not any more (SetWindowPos; Qt's flag would make the window anew)"""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return
    hwnd_topmost, hwnd_notopmost = -1, -2
    swp_nosize, swp_nomove, swp_noactivate = 0x1, 0x2, 0x10
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos(ctypes.c_void_p(hwnd), hwnd_topmost if on else hwnd_notopmost, 0, 0, 0, 0,
                        swp_nosize | swp_nomove | swp_noactivate)
