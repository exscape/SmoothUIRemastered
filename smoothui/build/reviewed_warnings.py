"""Patcher warnings (see patcher/swf_patcher/report.py) that have been checked by hand.

A review is a warning's key and the hash of the code it is about, e.g.
"unresolved-goto Class.method @1a2b3c4d". The key names a code location rather than a file, so one
review covers every file that contains that code; the hash ties the review to the code that was
checked, so it no longer applies once that code changes (in a game update, or in another mod's
version of a file). A key can be reviewed for several hashes.

Main builds read config/reviewed_warnings.txt: one review per line, "#" starting a comment.
build-main --accept-warnings adds the current unreviewed ones.

Compat builds don't use that file. They hide the warnings the Smooth UI release recorded for each
file (only where the other mod has the same code), plus those marked "reviewed:" in the override
file, so only warnings that the other mod's changes introduced are shown.
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from colorama import Back, Fore, Style

from ..common import fail, profile_key
from .shared import REVIEWED_WARNINGS

REVIEW = re.compile(r"\S+ \S+ @[0-9a-f]{8}")


def review_of(warning: dict) -> str:
    """The review that covers a warning (a report's warning dict): its key and code hash"""
    return f"{warning['key']} @{warning.get('hash')}"


def check_review(text: str) -> str:
    """text, if it is a review; raises ValueError otherwise"""
    if not REVIEW.fullmatch(text):
        raise ValueError(f"'{text}' is not a warning review: expected '<kind> <Class.method> @<hash>', "
                         "as the build prints it")
    return text


class WarningSummary(NamedTuple):
    unreviewed: dict[str, list[str]]    # review that would cover it -> "file: message" for each occurrence
    hidden: int                         # occurrences of reviewed warnings
    stale: list[str]                    # reviews that no longer match a warning in any file
    unchecked: int                      # files that could not be checked, so stale may be wrong


class WarningReview:
    def __init__(self, reviewed=(), recorded=None):
        """reviewed: reviews for every file; recorded: {profile key: reviews} for that file only"""
        self.reviewed = set(reviewed)
        self.recorded = recorded or {}

    def is_reviewed(self, rel, warning: dict) -> bool:
        review = review_of(warning)
        return review in self.reviewed or review in self.recorded.get(profile_key(rel), ())

    def summarize(self, entries, unchecked=0) -> WarningSummary:
        """Check the warnings in the reports of patch_files' patched (and kept) lists. unchecked: the
        number of files that have no report (failed), whose warnings are unknown"""
        unreviewed, hidden, seen = defaultdict(list), 0, set()
        for entry in entries:
            for warning in entry["report"]["warnings"]:
                seen.add(review_of(warning))
                if self.is_reviewed(entry["path"], warning):
                    hidden += 1
                else:
                    unreviewed[review_of(warning)].append(f"{entry['path']}: {warning['message']}")
        return WarningSummary(dict(unreviewed), hidden, sorted(self.reviewed - seen), unchecked)


def load_reviewed(path: Path = REVIEWED_WARNINGS) -> set[str]:
    if not path.is_file():
        return set()
    reviewed = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
        try:
            if line:
                reviewed.add(check_review(line))
        except ValueError as e:
            fail(f"{path}:{number}: {e}")
    return reviewed


def accept_warnings(unreviewed: dict[str, list[str]], path: Path = REVIEWED_WARNINGS):
    """Add reviews to the reviewed list, each with a comment naming the files it occurs in"""
    with open(path, "a", encoding="utf-8") as f:
        for review, occurrences in sorted(unreviewed.items()):
            files = sorted({Path(occurrence.split(":", 1)[0]).name for occurrence in occurrences})
            f.write(f"{review}    # {', '.join(files)}\n")
    print(f"Added {len(unreviewed)} review(s) to {path}; consider noting why each one is harmless")


def print_warning_summary(summary: WarningSummary, how_to_accept: list[str], reviewed_source: str):
    """how_to_accept: lines telling the user how to mark the unreviewed warnings as reviewed"""
    if summary.hidden:
        print(f"{summary.hidden} reviewed warning(s) hidden", flush=True)
    if summary.stale and summary.unchecked:
        # The unseen warnings may well occur in the files that failed
        print(f"{Fore.YELLOW}Skipped the check for reviewed warnings that no longer occur, as "
              f"{summary.unchecked} file(s) could not be patched{Style.RESET_ALL}", flush=True)
    elif summary.stale:
        print(f"{Fore.YELLOW}{len(summary.stale)} review(s) no longer match a warning; "
              f"consider removing them from {reviewed_source}:{Style.RESET_ALL}", flush=True)
        for key in summary.stale:
            print(f"{Fore.YELLOW}  {key}{Style.RESET_ALL}", flush=True)
    if not summary.unreviewed:
        return
    print(f"\n{Back.YELLOW}{Fore.BLACK} {len(summary.unreviewed)} UNREVIEWED WARNING(S): "
          f"check these by hand {Style.RESET_ALL}", flush=True)
    for review, occurrences in summary.unreviewed.items():
        print(f"{Fore.YELLOW}  {review}{Style.RESET_ALL}", flush=True)
        for occurrence in occurrences:
            print(f"{Fore.YELLOW}    {occurrence}{Style.RESET_ALL}", flush=True)
    for line in how_to_accept:
        print(line, flush=True)
    print(flush=True)
