"""The words the merger gives a file's outcome: what a build decided for it (FileStatus, written to the package's
manifest), why its rescale has problems (Reason), why the launch check finds it as it does (Cause), why a scan
builds it from the game's copy (FromGame), and how bad a build's outcome for it is (Level).

StrEnums: each member is its text, so the files written hold the same words, and a member equals its text.
Light: the launch check (state.py) imports this, and policy.py, which imports the patcher, takes FileStatus
from here.
"""

from __future__ import annotations

from enum import StrEnum


class FileStatus(StrEnum):
    """What a build decided for a file (policy.decide), as the package's manifest records it"""
    PATCHED = "patched"             # in the package
    FORCED = "forced"               # in the package although its rescale had problems, as the user asked
    LEFT_OUT = "left out"           # its rescale had problems, so it isn't in the package (it can be forced)
    KNOWN_BROKEN = "known broken"   # its mod's copy is known not to work with Smooth UI, so it isn't in the
                                    # package (it can be forced)
    UNCHANGED = "unchanged"         # nothing changed it: its own frame rate is kept (at or above the target, or
                                    # already rescaled, see patcher_core.ALREADY_RESCALED_FPS), and each of its
                                    # fixes wasn't needed (or can't apply): not needed in the package
    FAILED = "failed"               # couldn't be patched, so it isn't in the package (it can't be forced)
    GROUP_LEFT_OUT = "group left out"   # left out, as its group can't be merged whole (see policy.py)
    # Not from policy.decide: a copy not merged, as the user chose (see scan.Scan.screens_off), or as our rule
    # says (a dont-merge line, see mod_reviews.py), that the package holds as it is (see builder.as_is_copies)
    BY_CHOICE = "by choice"
    BY_RULE = "by rule"

    @property
    def included(self) -> bool:
        return self in (FileStatus.PATCHED, FileStatus.FORCED)

    @classmethod
    def read(cls, value) -> FileStatus:
        """A status read from a manifest; one from newer tools counts as LEFT_OUT"""
        try:
            return cls(value)
        except ValueError:
            return cls.LEFT_OUT


class Reason(StrEnum):
    """Why a file's rescale has problems (policy.PackageFile.reason; see state.WHY)"""
    KNOWN_BROKEN = "known broken"   # a warning that means its copy doesn't work
    CODE = "code"                   # a warning that isn't reviewed
    CHANGES = "changes"             # a setting that wasn't followed, or no longer matches

    @classmethod
    def read(cls, value) -> Reason | None:
        """A reason read from a manifest; None for none (records of older mergers), or one from newer tools"""
        try:
            return cls(value)
        except ValueError:
            return None


class Cause(StrEnum):
    """Why the launch check finds a file as it does, in a word, for the window to group files by: state.FileState.cause,
    which can also be a FileStatus (see there). No value is also a FileStatus's."""
    SMOOTHUI = "smoothui"           # Smooth UI was updated since the build
    MERGER = "merger"               # the merger was updated since the build
    FPS = "fps"                     # the file's frame rate changed since the build
    UPDATED = "updated"             # the mod's copy changed
    LOAD_ORDER = "load order"       # another mod's copy wins now
    SMOOTHUI_FIRST = "smoothui first"   # the game shows Smooth UI's copy, where the package should hold the mod's
                                        # as it is
    SETTING = "setting"             # its group was turned off or on since the build
    OPTIONAL = "optional"           # an optional fix for it was chosen or dropped since the build
    GAME = "game"                   # the game's copy changed since the installed build's release
    TURNED_OFF = "turned off"       # its group is turned off
    FIXES = "fixes"                 # merged without some fixes


class FromGame(StrEnum):
    """Why a scan builds a file from the game's copy, where no other mod ships it (scan.Scan.from_game)"""
    MERGER_ONLY = "merger-only"     # no download holds it
    FPS = "fps"                     # the menus' rate isn't the installed build's
    GAME = "game"                   # the game's copy changed since the installed build's release
    OPTIONAL = "optional"           # an optional fix was chosen for it


class Level(StrEnum):
    """How a build's outcome for a file is told (policy.PackageFile.severity), and logged"""
    OK = "ok"               # as Smooth UI would patch it, or patched anyway as the user chose, or known not to work
    WARNING = "warning"     # without some fixes
    ERROR = "error"         # not in the package, although it needs to be
