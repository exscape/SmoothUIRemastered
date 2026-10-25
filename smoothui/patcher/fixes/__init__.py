"""Fixes for bugs in the game's own SWFs (present without any mod) or caused by the rescale, and features
Smooth UI adds to them.

A profile enables them per file, with "fix: NAME" (see config/profile_default.txt). Each fix is a
module here, with NAME, KIND, CONSEQUENCE, apply(swf: SwfEdit) -> FixResult and optionally PHASE (see
swf_edit.py: most fixes run before the rescale). apply raises CantApply if the file isn't as the fix
expects, and returns no sprites if there is nothing to fix (the bug is fixed in this version of the file).
KIND (a FixKind) and CONSEQUENCE tell users what not having the fix means, so that they can choose
whether to use a file the fix can't apply to (see merger/policy.py). A message shows the kind's header,
then CONSEQUENCE: one sentence without a full stop, starting lowercase unless with a name, e.g. "Game
bug fix missing: in Gwent, the falling rain stops for a frame on every loop".

Releases record the fixes they applied in their manifests, and the merger and compat builds for a
release apply them again, so a fix's NAME also goes in smoothui/requires.py (older mergers refuse a
release that requires a fix they don't have). Never rename a fix. Removing one (e.g. once the game fixes
the bug) is fine, but releases that used it can then no longer be merged or get compat builds: move its
requirement to RETIRED there, so the merger tells users to update Smooth UI.
"""

from __future__ import annotations

from ...requires import FIX_NAMES
from . import (
    gwent_3d_bug_fix,
    gwent_card_fx_filter_fix,
    gwent_coin_shadow_fix,
    gwent_fog_intro_fix,
    gwent_frost_flicker_fix,
    gwent_rain_loop_fix,
    gwent_speed_options,
    hub_tab_hover_fix,
)
from .swf_edit import (
    AFTER_RESCALE,
    BEFORE_RESCALE,
    CantApply,
    FixKind,
    FixResult,
    SwfEdit,
)

FIXES = {fix.NAME: fix for fix in (gwent_3d_bug_fix, gwent_card_fx_filter_fix, gwent_coin_shadow_fix, gwent_fog_intro_fix,
                                   gwent_frost_flicker_fix, gwent_rain_loop_fix, gwent_speed_options,
                                   hub_tab_hover_fix)}
if set(FIXES) != FIX_NAMES:
    raise RuntimeError(f"requires.py's FIX_NAMES must list exactly the fixes: {', '.join(sorted(FIXES))}")

# A fix's "result" in its report record
APPLIED, NOT_NEEDED, CANT_APPLY = "applied", "not needed", "can't apply"


def check_fix_name(name: str):
    """ValueError unless a fix with this name exists"""
    if name not in FIXES:
        raise ValueError(f"unknown fix '{name}' (known fixes: {', '.join(FIXES)})")


def fix_consequence(name: str) -> str:
    """What not having a fix means (its CONSEQUENCE)"""
    check_fix_name(name)
    return FIXES[name].CONSEQUENCE


def fix_kind(name: str) -> FixKind:
    """What a fix is (its KIND)"""
    check_fix_name(name)
    return FIXES[name].KIND


def apply_fixes(body: bytes, names, phase: str) -> tuple[bytes | None, list[dict]]:
    """Apply those of the named fixes that run in phase (BEFORE_RESCALE or AFTER_RESCALE), in order,
    to a decompressed SWF body.

    Returns (the new body, or None if nothing changed, a record per fix applied). A record is
    {"name", "result": APPLIED / NOT_NEEDED / CANT_APPLY, "matched": [ids of the sprites it changed],
    "detail": what it did, or why not}."""
    for name in names:
        check_fix_name(name)
    names = [name for name in names if getattr(FIXES[name], "PHASE", BEFORE_RESCALE) == phase]
    if not names:
        return None, []
    swf = SwfEdit(body)
    records = []
    for name in names:
        try:
            result = FIXES[name].apply(swf)
        except CantApply as e:
            records.append({"name": name, "result": CANT_APPLY, "matched": [], "detail": str(e)})
            continue
        records.append({"name": name, "result": APPLIED if result.sprites else NOT_NEEDED,
                        "matched": sorted(result.sprites), "detail": result.detail})
    return (swf.body() if swf.changed else None), records


__all__ = ["AFTER_RESCALE", "APPLIED", "BEFORE_RESCALE", "CANT_APPLY", "FIXES", "NOT_NEEDED", "CantApply", "FixKind", "FixResult",
           "SwfEdit", "apply_fixes", "check_fix_name", "fix_consequence", "fix_kind"]
