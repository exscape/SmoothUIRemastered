"""Output for every Smooth UI tool, through the standard logging module.

Code that reports something logs it to its module's logger, and never decides where it goes:

    log = logging.getLogger(__name__)
    log.info("Packed 12 files")
    log.warning("...")                      # the level gives the color (and stream) on the console
    log.info("Packing", extra=HEADING)      # a step's header; SUBHEADING for a smaller one

Only an entry point decides where output goes, by adding handlers to the root logger: setup_console() for
the command line (see run_cli() in common.py); a GUI adds its own. (The root logger, so that a script's
logging.getLogger(__name__), which is "__main__", is shown too.)
"""

import logging
import sys

from colorama import Back, Fore, Style, just_fix_windows_console

HEADING = {"heading": 1}
SUBHEADING = {"heading": 2}

LEVEL_COLORS = {logging.DEBUG: Style.DIM, logging.WARNING: Fore.YELLOW, logging.ERROR: Style.BRIGHT + Fore.RED}
LEVEL_PREFIXES = {logging.WARNING: "WARNING: ", logging.ERROR: "ERROR: "}
HEADING_COLORS = {logging.WARNING: Back.YELLOW + Fore.BLACK, logging.ERROR: Back.RED + Fore.WHITE}

class ConsoleFormatter(logging.Formatter):
    """Colors by level; headers as banners of = (HEADING) or - (SUBHEADING)"""
    def format(self, record):
        msg = record.getMessage()
        # The standard level at or below the record's (CRITICAL looks like ERROR)
        level = next((lv for lv in (logging.ERROR, logging.WARNING, logging.INFO) if record.levelno >= lv), logging.DEBUG)
        heading = getattr(record, "heading", None)
        if heading == 1:
            color = HEADING_COLORS.get(level, Back.BLACK + Fore.GREEN)
            return f"\n{color}{_banner(msg, '=', 130)}{Style.RESET_ALL}"
        if heading == 2:
            color = HEADING_COLORS.get(level, Fore.CYAN)
            return f"\n{color}{_banner(msg, '-', 90, align_start=True)}{Style.RESET_ALL}"
        prefix = LEVEL_PREFIXES.get(level)
        if prefix:  # after any leading blank lines and indent, so "  item" stays indented
            text = msg.lstrip()
            msg = msg[:len(msg) - len(text)] + prefix + text
        color = LEVEL_COLORS.get(level)
        if not color:
            return msg
        return "\n".join(f"{color}{line}{Style.RESET_ALL}" if line else line for line in msg.split("\n"))

def _banner(msg, symbol, width, align_start=False):
    if align_start:
        text = f"{msg} {symbol * max(width - 1 - len(msg), 5)}"
    else:
        symbols = symbol * max(5, (width - len(msg) - 2) // 2)
        text = f"{symbols} {msg} {symbols}"
    if len(text) % 2 != 0:
        text += symbol
    return text

class _StderrHandler(logging.StreamHandler):
    """Errors go to stderr; stdout is flushed first, so they show up in order"""
    def emit(self, record):
        sys.stdout.flush()
        super().emit(record)

def setup_console():
    """Log to the console: errors to stderr, everything else to stdout; our own debug messages too, but
    other libraries' only from INFO up. Does nothing without a console (a windowed exe has no sys.stdout)."""
    if sys.stdout is None or sys.stderr is None:
        return
    just_fix_windows_console()  # first: it may replace sys.stdout and sys.stderr
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(lambda record: record.levelno < logging.ERROR)
    err = _StderrHandler(sys.stderr)
    err.setLevel(logging.ERROR)
    root = logging.getLogger()
    for handler in (out, err):
        handler.setFormatter(ConsoleFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("smoothui").setLevel(logging.DEBUG)
