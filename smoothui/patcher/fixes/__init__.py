"""Fixes for bugs in the game's own SWFs (present without any mod).

A profile enables them per file, with "fix: NAME" (see config/profile_default.txt). Each fix is a
module here, with NAME, apply(swf: SwfEdit) -> FixResult and optionally PHASE (see swf_edit.py: most
fixes run before the rescale). apply raises CantApply if the file isn't as the fix expects, and
returns no sprites if there is nothing to fix (the bug is fixed in this version of the file).

Releases record the fixes they applied in their manifests, and compat builds for a release apply
them again: don't remove or rename a fix that a manifest in manifests/ names.
"""

from __future__ import annotations

from . import (
    gwent_3d_bug_fix,
    gwent_coin_shadow_fix,
    gwent_fog_intro_fix,
    gwent_frost_flicker_fix,
    gwent_rain_loop_fix,
    gwent_speed_options,
    hub_tab_hover_fix,
)
from .swf_edit import AFTER_RESCALE, BEFORE_RESCALE, CantApply, FixResult, SwfEdit

FIXES = {fix.NAME: fix for fix in (gwent_3d_bug_fix, gwent_coin_shadow_fix, gwent_fog_intro_fix,
                                   gwent_frost_flicker_fix, gwent_rain_loop_fix, gwent_speed_options,
                                   hub_tab_hover_fix)}

# A fix's "result" in its report record
APPLIED, NOT_NEEDED, CANT_APPLY = "applied", "not needed", "can't apply"


def check_fix_name(name: str):
    """ValueError unless a fix with this name exists"""
    if name not in FIXES:
        raise ValueError(f"unknown fix '{name}' (known fixes: {', '.join(FIXES)})")


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


__all__ = ["AFTER_RESCALE", "APPLIED", "BEFORE_RESCALE", "CANT_APPLY", "FIXES", "NOT_NEEDED", "CantApply", "FixResult",
           "SwfEdit", "apply_fixes", "check_fix_name"]
