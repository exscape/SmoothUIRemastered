"""Reviews of patcher warnings in other mods' code, for the merger: warnings checked by hand and found
harmless (see build/reviewed_warnings.py for reviews in general).

The game's own code is reviewed in config/reviewed_warnings.txt, and each release's manifest records the
warnings each of its files had, so the merger hides those where a mod's copy has the same code. Warnings in
code that a mod added or changed are reviewed here instead, in manifests/MOD_REVIEWS. It ships with the
tools, and is uploaded to the "manifests" GitHub release too, from which the update check downloads it
(see merger/updates.py): new reviews reach the merger without a release. A downloaded copy is used
instead of the shipped one.

The file groups reviews under a header naming the mods they are for: a regular expression, then ":". It
must match a mod's folder name in full, ignoring case (a mod may ship as one folder, or as several):

    # Ink and Iron
    modInkAndIron.*:
        enter-frame-handler MapMenu.inkOnFrame @fd85b1ea
        enter-frame-handler TopBarNew.inkTick @any

A review is "<kind> <Class.method> @<hash>", as the merger's log shows it, or "@any": any version of that
code, for code that is harmless by design (so that the mod's updates need no new review). "#" starts a
comment. A line that can't be read is logged and skipped, so that a file written for newer tools never
stops older ones.

Light: the launch check reads which version of the file is used (sha256).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .common import SmoothUIError
from .files import read_file
from .paths import MANIFEST_DIRS

log = logging.getLogger(__name__)

MOD_REVIEWS = "reviewed_mod_warnings.txt"
# A review's hash for any version of the code
ANY = "any"
REVIEW = re.compile(rf"\S+ \S+ @(?:[0-9a-f]{{8}}|{ANY})")


@dataclass(frozen=True, slots=True)
class ModReviews:
    groups: tuple[tuple[re.Pattern, frozenset[str]], ...] = ()   # (the mods' pattern, their reviews)
    sha256: str | None = None   # load(): of the file read (None: none), which a package records (see sha256())

    def for_mod(self, folder: str) -> set[str]:
        """The reviews for a mod's copies of files, by its folder name"""
        return {review for pattern, reviews in self.groups if pattern.fullmatch(folder) for review in reviews}


def path() -> Path | None:
    """The file in use: the downloaded copy if there is one, else the shipped one; None if there is neither"""
    return next((found for folder in reversed(MANIFEST_DIRS) if (found := folder / MOD_REVIEWS).is_file()), None)


def sha256() -> str | None:
    """The SHA-256 of the file in use (None: there is none, or it can't be read). A package records the one
    of the reviews it was built with (ModReviews.sha256), so that the launch check can tell when they changed
    since."""
    found = path()
    try:
        data = read_file(found) if found else None
    except SmoothUIError:
        return None
    return hashlib.sha256(data).hexdigest() if data is not None else None


def load() -> ModReviews:
    """The reviews in the file in use (none if there is none). Lines that can't be read are logged."""
    found = path()
    try:
        data = read_file(found) if found else None
    except SmoothUIError as e:
        log.warning(f"The reviewed warnings of mods' code can't be read: {e}")
        return ModReviews()
    if data is None:
        return ModReviews()
    reviews, problems = parse(data.decode("utf-8", errors="replace"))
    for problem in problems:
        log.warning(f"{found}: {problem}")
    return replace(reviews, sha256=hashlib.sha256(data).hexdigest())


def parse(text: str) -> tuple[ModReviews, list[str]]:
    """(the reviews in a file's text, a line for each problem)"""
    groups, problems = [], []
    group = None    # [pattern, reviews] of the last header; its pattern is None if it isn't valid
    for number, line in enumerate(text.splitlines(), 1):
        line = " ".join(re.split(r"(?:^|\s)#", line, maxsplit=1)[0].split())
        if not line:
            continue
        if line.endswith(":"):
            try:
                group = [re.compile(line[:-1].strip(), re.IGNORECASE), set()]
            except re.error as e:
                problems.append(f"line {number}: '{line[:-1].strip()}' isn't a valid regular expression ({e}), so "
                                "the reviews below it are skipped")
                group = [None, set()]
            groups.append(group)
        elif group is None:
            problems.append(f"line {number}: a review before the first header ('<mod folder pattern>:'): skipped")
        elif not REVIEW.fullmatch(line):
            problems.append(f"line {number}: '{line}' isn't a review ('<kind> <Class.method> @<hash or {ANY}>'): "
                            "skipped")
        else:
            group[1].add(line)
    return ModReviews(tuple((pattern, frozenset(reviews)) for pattern, reviews in groups
                            if pattern is not None and reviews)), problems
