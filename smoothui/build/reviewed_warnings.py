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
file, so only warnings that the other mod's changes introduced are shown. The merger hides the same
recorded warnings, plus the reviews of mods' own code in mod_reviews.py's file; there, a review's hash may
be "any" (any version of that code).
"""

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from ..common import fail, profile_key
from ..log import SUBHEADING
from ..mod_reviews import ANY
from .shared import REVIEWED_WARNINGS

log = logging.getLogger(__name__)

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


class Occurrence(NamedTuple):
    """One occurrence of a warning; str() gives it as "file: message" """
    path: str           # the file, as patch_files names it
    message: str

    def __str__(self):
        return f"{self.path}: {self.message}"


class WarningSummary(NamedTuple):
    unreviewed: dict[str, list[Occurrence]]     # review that would cover it -> each occurrence
    hidden: int                         # occurrences of reviewed warnings
    stale: list[str]                    # reviews that no longer match a warning in any file
    unchecked: int                      # files that could not be checked, so stale may be wrong


class WarningReview:
    def __init__(self, reviewed=(), recorded=None):
        """reviewed: reviews for every file; recorded: {profile key: reviews} for that file only"""
        self.reviewed = set(reviewed)
        self.recorded = recorded or {}

    def is_reviewed(self, rel, warning: dict) -> bool:
        recorded = self.recorded.get(profile_key(rel), ())
        return any(review in self.reviewed or review in recorded
                   for review in (review_of(warning), f"{warning['key']} @{ANY}"))

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
                    unreviewed[review_of(warning)].append(Occurrence(entry["path"], warning["message"]))
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


def accept_warnings(unreviewed: dict[str, list[Occurrence]], path: Path = REVIEWED_WARNINGS):
    """Add reviews to the reviewed list, each with a comment naming the files it occurs in"""
    with open(path, "a", encoding="utf-8") as f:
        for review, occurrences in sorted(unreviewed.items()):
            files = sorted({Path(occurrence.path).name for occurrence in occurrences})
            f.write(f"{review}    # {', '.join(files)}\n")
    log.info(f"Added {len(unreviewed)} review(s) to {path}; consider noting why each one is harmless")


def print_warning_summary(summary: WarningSummary, how_to_accept: list[str], reviewed_source: str):
    """how_to_accept: lines telling the user how to mark the unreviewed warnings as reviewed"""
    if summary.hidden:
        log.info(f"{summary.hidden} reviewed warning(s) hidden")
    if summary.stale and summary.unchecked:
        # The unseen warnings may well occur in the files that failed
        log.warning(f"Skipped the check for reviewed warnings that no longer occur, as "
                    f"{summary.unchecked} file(s) could not be patched")
    elif summary.stale:
        log.warning(f"{len(summary.stale)} review(s) no longer match a warning; consider removing them from "
                    f"{reviewed_source}:" + "".join(f"\n  {key}" for key in summary.stale))
    if not summary.unreviewed:
        return
    log.warning(f"{len(summary.unreviewed)} UNREVIEWED WARNING(S): check these by hand", extra=SUBHEADING)
    for review, occurrences in summary.unreviewed.items():
        log.warning(f"  {review}" + "".join(f"\n    {occurrence}" for occurrence in occurrences))
    for line in how_to_accept:
        log.info(line)
    log.info("")
