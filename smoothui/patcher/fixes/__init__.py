"""Fixes for bugs in the game's own SWFs (present without any mod) or caused by the rescale, and features
Smooth UI adds to them.

A profile enables them per file, with "fix: NAME" (see config/profile_default.txt), or the user does in the
merger's settings (see requires.OPTIONAL_FIXES). Each fix is a module here that defines FIX, a Fix (see
swf_edit.py), and is listed in FIXES below. Its apply(swf: SwfEdit) -> FixResult raises CantApply if the file isn't as the fix expects, and returns no sprites if there is nothing
to fix (the bug is fixed in this version of the file). Its phase says when it runs (most run before the
rescale). Its kind (a FixKind) and consequence tell users what not having the fix means, so that they can
choose whether to use a file the fix can't apply to (see merger/policy.py). A message shows the kind's
header, then the consequence: one sentence without a full stop, starting lowercase unless with a name, e.g.
"Game bug fix missing: in Gwent, the falling rain stops for a frame on every loop".

Releases record the fixes they applied in their manifests, and the merger and compat builds for a
release apply them again, so a fix's name also goes in smoothui/requires.py (older mergers refuse a
release that requires a fix they don't have). Never rename a fix. Removing one (e.g. once the game fixes
the bug) is fine, but releases that used it can then no longer be merged or get compat builds: move its
requirement to RETIRED there, so the merger tells users to update Smooth UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ...requires import FIX_NAMES
from . import (
    book_fade_fix,
    book_stick_scroll_fix,
    gwent_3d_bug_fix,
    gwent_card_fx_filter_fix,
    gwent_coin_shadow_fix,
    gwent_flipbook_fix,
    gwent_fog_intro_fix,
    gwent_frost_flicker_fix,
    gwent_rain_loop_fix,
    gwent_speed_options,
    health_bar_glow_fix,
    hub_tab_hover_fix,
    menu_slide_fix,
    text_stick_scroll_fix,
)
from .swf_edit import (
    CantApply,
    Fix,
    FixKind,
    FixResult,
    Phase,
    SwfEdit,
)

FIXES = {module.FIX.name: module.FIX for module in (
    book_fade_fix, book_stick_scroll_fix, gwent_3d_bug_fix, gwent_card_fx_filter_fix, gwent_coin_shadow_fix,
    gwent_flipbook_fix, gwent_fog_intro_fix, gwent_frost_flicker_fix, gwent_rain_loop_fix, gwent_speed_options,
    health_bar_glow_fix, hub_tab_hover_fix, menu_slide_fix, text_stick_scroll_fix)}
if set(FIXES) != FIX_NAMES:
    raise RuntimeError(f"requires.py's FIX_NAMES must list exactly the fixes: {', '.join(sorted(FIXES))}")


class FixOutcome(StrEnum):
    """A fix's "result" in its report record"""
    APPLIED = "applied"
    NOT_NEEDED = "not needed"       # the file has no bug to fix (fixed in this version of it)
    CANT_APPLY = "can't apply"      # the file isn't as the fix expects


@dataclass(frozen=True, slots=True)
class FixRecord:
    """What one fix did to a file; to_json gives its record in the file's rescale report"""
    name: str
    outcome: FixOutcome
    matched: list[int]      # ids of the sprites it changed
    detail: str             # what it did, or why not

    def to_json(self) -> dict:
        return {"name": self.name, "result": self.outcome, "matched": self.matched, "detail": self.detail}


def check_fix_name(name: str):
    """ValueError unless a fix with this name exists"""
    if name not in FIXES:
        raise ValueError(f"unknown fix '{name}' (known fixes: {', '.join(FIXES)})")


def get_fix(name: str) -> Fix:
    """The fix with this name (ValueError if there is none)"""
    check_fix_name(name)
    return FIXES[name]


def apply_fixes(body: bytes, names, phase: Phase) -> tuple[bytes | None, list[FixRecord]]:
    """Apply those of the named fixes that run in phase, in order, to a decompressed SWF body.

    Returns (the new body, or None if nothing changed, a record per fix applied)."""
    for name in names:
        check_fix_name(name)
    names = [name for name in names if FIXES[name].phase is phase]
    if not names:
        return None, []
    swf = SwfEdit(body)
    records = []
    for name in names:
        try:
            result = FIXES[name].apply(swf)
        except CantApply as e:
            records.append(FixRecord(name, FixOutcome.CANT_APPLY, [], str(e)))
            continue
        records.append(FixRecord(name, FixOutcome.APPLIED if result.sprites else FixOutcome.NOT_NEEDED,
                                 sorted(result.sprites), result.detail))
    return (swf.body() if swf.changed else None), records


__all__ = ["FIXES", "CantApply", "Fix", "FixKind", "FixOutcome", "FixRecord", "FixResult", "Phase", "SwfEdit",
           "apply_fixes", "check_fix_name", "get_fix"]
