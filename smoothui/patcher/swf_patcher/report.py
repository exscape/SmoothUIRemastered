"""The rescale report: a JSON-compatible dict, and its human-readable text form.

The dict only holds plain values (str, int, float, bool, None, lists and dicts), so it survives a
round trip through JSON unchanged. Everything that varies per sprite is in the "sprites" list, one
record per multi-frame sprite, sorted by id; records carry both the id and the linkage class name,
so reports of two versions of a file can be matched up even if ids shift.

    {
      "old_fps": 22.0, "new_fps": 120.0,    # new_fps is None if the file was left unchanged
      "target_fps": 120.0,
      "ratio": "60/11", "holds": [5, 6],    # each original frame lasts 5 or 6 new frames
      "rules": [{"pattern": "...", "fps": 80.0, "ratio": "40/11", "holds": [3, 4], "matched": [338]}],
      # Explicit rules (fps rules, include) that were NOT followed; should be empty
      "unmet_rules": [{"rule": "fps rule '...' (80 fps)", "id": 338, "class": "...", "problem": "..."}],
      "patched": {"frame_scripts": 88, "gotos": 1, "increments": 0},
      "increments": [{"handler": "Class.method", "property": "rotation", "old": 5.0, "new": 0.92}],
      "sprites": [
        {"id": 315, "class": "Foo", "frames": 80, "stretched": true, "fps": 120.0, "new_frames": 436,
         "interpolation": {"steps": 8, "snapped": 0, "bytes": 407, "motion": 200.0}},
        {"id": 517, "class": "Bar", "frames": 100, "stretched": false, "reasons": ["..."],
         "probably_safe_labels": ["blue", "red"]},
      ],
      "unlinked_classes": ["..."],
      "warnings": ["..."],
    }

"interpolation" and "probably_safe_labels" are only present where they apply. A report for a file
that was left unchanged only has old_fps, new_fps, target_fps and unmet_rules. A sprite's "class"
is None if it has no linkage class.
"""

from __future__ import annotations

from fractions import Fraction

from .movie import SwfMovie
from .options import RescaleOptions
from .planner import RescalePlan
from .retiming import Retiming, fps_ratio


def unchanged_report(old_fps: float, target_fps: float) -> dict:
    return {"old_fps": old_fps, "new_fps": None, "target_fps": target_fps, "unmet_rules": []}


def build_report(movie: SwfMovie, options: RescaleOptions, plan: RescalePlan) -> dict:
    rules = []
    for rule, matched in zip(options.rules, plan.rule_matches):
        retiming = Retiming(fps_ratio(plan.source_fps, rule.fps))
        rules.append({"pattern": rule.pattern.pattern, "fps": float(rule.fps),
                      "ratio": str(retiming.ratio), "holds": retiming.hold_lengths(), "matched": matched})

    return {
        "old_fps": plan.source_fps,
        "new_fps": float(plan.target_fps),
        "target_fps": float(plan.target_fps),
        "ratio": str(plan.retiming.ratio),
        "holds": plan.retiming.hold_lengths(),
        "rules": rules,
        "unmet_rules": [{"rule": unmet.rule, "id": unmet.character_id,
                         "class": movie.class_of_sprite.get(unmet.character_id), "problem": unmet.problem}
                        for unmet in plan.unmet_rules],
        "patched": {
            "frame_scripts": plan.frame_script_edits,
            "gotos": plan.goto_edits,
            "increments": len(plan.increments),
        },
        "increments": [{"handler": inc.handler, "property": inc.prop, "old": inc.old, "new": inc.new}
                       for inc in plan.increments],
        "sprites": [_sprite_record(movie, plan, sprite)
                    for sprite in sorted(movie.multi_frame_sprites(), key=lambda s: s.character_id)],
        "unlinked_classes": sorted(plan.unlinked_classes),
        "warnings": list(plan.warnings),
    }


def _sprite_record(movie: SwfMovie, plan: RescalePlan, sprite) -> dict:
    cid = sprite.character_id
    record = {"id": cid, "class": movie.class_of_sprite.get(cid), "frames": sprite.frame_count}

    retiming = plan.sprite_retiming.get(cid)
    if retiming is None:
        record["stretched"] = False
        record["reasons"] = list(dict.fromkeys(plan.reasons[cid]))
        if cid in plan.probably_safe:
            record["probably_safe_labels"] = plan.probably_safe[cid]
        return record

    record.update(stretched=True, fps=float(plan.sprite_fps[cid]), new_frames=retiming.tick(sprite.frame_count))
    tweens = plan.tweens.get(cid)
    if tweens:
        record["interpolation"] = {"steps": tweens.stats.interpolated, "snapped": tweens.stats.snapped,
                                   "bytes": tweens.added_bytes, "motion": round(tweens.motion, 2)}
    return record


def format_unmet_rule(unmet: dict) -> str:
    return f"{unmet['rule']}: sprite {unmet['id']} {unmet['class'] or '(no class)'}: {unmet['problem']}"


def report_to_text(report: dict) -> str:
    """Human-readable version of a report dict (as returned by build_report, or loaded from JSON)"""
    if report["new_fps"] is None:
        return (f"source {report['old_fps']:.3f} fps is already at or above the target "
                f"{report['target_fps']:.3f} fps; left unchanged")

    lines = []
    out = lines.append
    if report["unmet_rules"]:
        out(f"!!! {len(report['unmet_rules'])} EXPLICIT RULE(S) NOT FOLLOWED:")
        lines.extend("!!!   " + format_unmet_rule(unmet) for unmet in report["unmet_rules"])
        out("")
    ratio = Fraction(report["ratio"])
    out(f"source {report['old_fps']:.3f} fps -> {report['new_fps']:.3f} fps   (R = {ratio} = {float(ratio):.4f})")
    out(f"holds per source frame: {report['holds']}")
    for rule in report["rules"]:
        rule_ratio = Fraction(rule["ratio"])
        out(f"fps rule '{rule['pattern']}': {rule['fps']:.3f} fps   (R = {rule_ratio} = {float(rule_ratio):.4f})   "
            f"holds per source frame: {rule['holds']}   matched sprites: {rule['matched'] or 'none'}")

    sprites = report["sprites"]
    stretched = [s for s in sprites if s["stretched"]]
    not_stretched = [s for s in sprites if not s["stretched"]]
    by_size = lambda s: (-s["frames"], s["id"])
    patched = report["patched"]
    out(f"multi-frame sprites: {len(sprites)}   stretched: {len(stretched)}   left alone: {len(not_stretched)}")
    out(f"patched: {patched['frame_scripts']} addFrameScript indices, {patched['gotos']} literal goto constants, "
        f"{patched['increments']} per-frame increments")
    for inc in report["increments"]:
        out(f"   per-frame {inc['property']} += {inc['old']:g} in {inc['handler']}  ->  {inc['new']:g}")

    out("")
    out("STRETCHED")
    for s in sorted(stretched, key=by_size):
        fps_note = f"   @ {s['fps']:g} fps" if s["fps"] != report["new_fps"] else ""
        out(f"  {s['id']:>4}  nf={s['frames']:<4} -> {s['new_frames']:<4} {s['class'] or ''}{fps_note}")

    out("")
    out("NOT STRETCHED (multi-frame)")
    for s in sorted(not_stretched, key=by_size):
        out(f"  {s['id']:>4}  nf={s['frames']:<4} {s['class'] or '-':<45} {'; '.join(s['reasons']) or '?'}")

    interpolated = [s for s in stretched if "interpolation" in s]
    if interpolated:
        out("")
        out("INTERPOLATION (tween steps get in-between ticks; keyframes stay bit-identical)")
        total_bytes = sum(s["interpolation"]["bytes"] for s in interpolated)
        out(f"  clips with tweens: {len(interpolated)}   raw bytes added: {total_bytes}")
        out(f"  {'id':>4} {'steps':>5} {'snaps':>6} {'rawB':>7} {'motion':>7}  class")
        value_per_byte = lambda s: -s["interpolation"]["motion"] / max(1, s["interpolation"]["bytes"])
        for s in sorted(interpolated, key=value_per_byte):
            interp = s["interpolation"]
            out(f"  {s['id']:>4} {interp['steps']:>5} {interp['snapped']:>6} {interp['bytes']:>7} "
                f"{interp['motion']:>7.0f}  {s['class'] or '-'}")

    probably_safe = [s for s in not_stretched if "probably_safe_labels" in s]
    if probably_safe:
        out("")
        out("PROBABLY SAFE TO STRETCH -- excluded only because an untyped value is passed to gotoAndStop/Play,")
        out("but their labels suggest the value is a label name.  Opt in with include_likely, or include them:")
        for s in probably_safe:
            out(f"  {s['id']:>4}  {s['class'] or '-':<45} labels: {', '.join(s['probably_safe_labels'])}")

    if report["unlinked_classes"]:
        out("")
        out(f"NOTE: {len(report['unlinked_classes'])} code classes with computed/literal frame jumps "
            "are not linked to any clip in this file (ignored)")

    if report["warnings"]:
        out("")
        out("REVIEW")
        lines.extend("  " + warning for warning in report["warnings"])
    return "\n".join(lines)
