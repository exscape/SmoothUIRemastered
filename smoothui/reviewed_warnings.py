"""Patcher warnings (see patcher/swf_patcher/report.py) that have been checked by hand.

Main builds read config/reviewed_warnings.txt: one warning key per line, "#" starting a comment.
build-main --accept-warnings adds the current unreviewed ones. A key names a code location rather
than a file, so one review covers every file that contains that code.

Compat builds don't use that file. They hide the warnings the Smooth UI release recorded for each
file, plus those marked "reviewed:" in the override file, so only warnings that the other mod's
changes introduced are shown.
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from colorama import Back, Fore, Style

from .build_common import REVIEWED_WARNINGS
from .common import profile_key


class WarningSummary(NamedTuple):
    unreviewed: dict[str, list[str]]    # warning key -> "file: message" for each occurrence
    hidden: int                         # occurrences of reviewed warnings
    stale: list[str]                    # reviewed keys that no longer occur in any file


class WarningReview:
    def __init__(self, reviewed=(), recorded=None):
        """reviewed: warning keys reviewed for every file; recorded: {profile key: warning keys}
        reviewed for that file only"""
        self.reviewed = set(reviewed)
        self.recorded = recorded or {}

    def is_reviewed(self, rel, key: str) -> bool:
        return key in self.reviewed or key in self.recorded.get(profile_key(rel), ())

    def summarize(self, entries) -> WarningSummary:
        """Check the warnings in the reports of patch_files' patched (and kept) lists"""
        unreviewed, hidden, seen = defaultdict(list), 0, set()
        for entry in entries:
            for warning in entry["report"]["warnings"]:
                seen.add(warning["key"])
                if self.is_reviewed(entry["path"], warning["key"]):
                    hidden += 1
                else:
                    unreviewed[warning["key"]].append(f"{entry['path']}: {warning['message']}")
        return WarningSummary(dict(unreviewed), hidden, sorted(self.reviewed - seen))


def load_reviewed(path: Path = REVIEWED_WARNINGS) -> set[str]:
    if not path.is_file():
        return set()
    lines = (re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
             for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line}


def accept_warnings(unreviewed: dict[str, list[str]], path: Path = REVIEWED_WARNINGS):
    """Add warning keys to the reviewed list, each with a comment naming the files it occurs in"""
    with open(path, "a", encoding="utf-8") as f:
        for key, occurrences in sorted(unreviewed.items()):
            files = sorted({Path(occurrence.split(":", 1)[0]).name for occurrence in occurrences})
            f.write(f"{key}    # {', '.join(files)}\n")
    print(f"Added {len(unreviewed)} warning key(s) to {path}; consider noting why each one is harmless")


def print_warning_summary(summary: WarningSummary, how_to_accept: list[str], reviewed_source: str):
    """how_to_accept: lines telling the user how to mark the unreviewed warnings as reviewed"""
    if summary.hidden:
        print(f"{summary.hidden} reviewed warning(s) hidden", flush=True)
    if summary.stale:
        print(f"{Fore.YELLOW}{len(summary.stale)} reviewed warning(s) no longer occur; "
              f"consider removing them from {reviewed_source}:{Style.RESET_ALL}", flush=True)
        for key in summary.stale:
            print(f"{Fore.YELLOW}  {key}{Style.RESET_ALL}", flush=True)
    if not summary.unreviewed:
        return
    print(f"\n{Back.YELLOW}{Fore.BLACK} {len(summary.unreviewed)} UNREVIEWED WARNING(S): "
          f"check these by hand {Style.RESET_ALL}", flush=True)
    for key, occurrences in summary.unreviewed.items():
        print(f"{Fore.YELLOW}  {key}{Style.RESET_ALL}", flush=True)
        for occurrence in occurrences:
            print(f"{Fore.YELLOW}    {occurrence}{Style.RESET_ALL}", flush=True)
    for line in how_to_accept:
        print(line, flush=True)
    print(flush=True)
