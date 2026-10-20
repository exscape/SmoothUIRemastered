"""Compare two release manifests (see shared.write_manifest), to see what a build changed: files added
or removed, and per file what changed in its settings, fixes, warnings, code edits and clip handling
(stretched or not, speed, interpolation). Only for information.

build-main.py --make-release compares each new manifest with the previous version's at the same frame
rate (v0.3-60fps with the 60 fps build before it); version-tool.py --diff compares any two.
Manifests from before the 60 and 120 fps builds count as 120 fps ones; their reports are in an older
format, so only their files are compared.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from ..common import fail, normalize_version
from ..log import HEADING
from ..patcher.swf_patcher import format_unmet_rule, report_settings
from .shared import (
    OLD_MANIFEST_FPS,
    build_fps,
    load_manifest,
    release_manifests,
    version_key,
)

log = logging.getLogger(__name__)

# What file_changes compares, in the order it reports them
FILE_ASPECTS = ("source", "frame rate", "settings", "fixes", "warnings", "unmet rules", "code edits", "clips")


def print_changes_since_previous(version):
    """Print what changed in a build's manifest since the previous version's at the same frame rate"""
    older = [path for other, path in release_manifests(build_fps(version)) if version_key(other) < version_key(version)]
    if not older:
        log.info(f"No {build_fps(version)} fps manifest of a version before {normalize_version(version)} to compare with")
        return
    print_manifest_diff(older[-1], version)


def print_manifest_diff(old, new):
    """Print what changed from manifest old to new (versions, or paths)"""
    manifests = []
    for version_or_path in (old, new):
        manifest = load_manifest(version_or_path)
        if manifest is None:
            fail(f"manifest not found: {version_or_path}")
        manifests.append(manifest)
    log.info(f"Changes from {manifests[0]['created_mod_version']} to {manifests[1]['created_mod_version']}", extra=HEADING)
    log.info("\n".join(diff_manifests(*manifests)))


def diff_manifests(old: dict, new: dict) -> list[str]:
    """What changed from manifest old to new, as lines of text"""
    changed, same = [], []
    for name, values in (("frame rate", [f"{manifest.get('fps', OLD_MANIFEST_FPS)} fps" for manifest in (old, new)]),
                         ("game version", [manifest.get("targeted_game_version") for manifest in (old, new)]),
                         ("patcher commit", [describe_commit(manifest) for manifest in (old, new)])):
        if values[0] != values[1]:
            changed.append(f"{name.capitalize()}: {values[0]} -> {values[1]}")
        else:
            same.append(f"{name} ({values[0]})")
    lines = changed + (["Same in both: " + ", ".join(same)] if same else [])

    # Reports from before the 60 and 120 fps builds (no top-level "fps") are in an older format
    compare_reports = "fps" in old and "fps" in new
    if not compare_reports:
        lines.append("A manifest is from before the 60 and 120 fps builds, so only the files' output is compared")

    old_files, new_files = old["files"], new["files"]
    removed, added = sorted(old_files.keys() - new_files.keys()), sorted(new_files.keys() - old_files.keys())
    if removed:
        lines += ["", f"Files removed ({len(removed)}):", *(f"  {path}" for path in removed)]
    if added:
        lines += ["", f"Files added ({len(added)}):", *(f"  {path} ({new_files[path]['fps']:g} fps)" for path in added)]

    unchanged = []
    for path in sorted(old_files.keys() & new_files.keys()):
        changes = file_changes(old_files[path], new_files[path], compare_reports)
        if changes:
            lines += ["", path, *("  " + change for change in changes)]
        else:
            unchanged.append(path)

    in_both = len(old_files.keys() & new_files.keys())
    stretched = [sum(sprite["stretched"] for record in manifest["files"].values()
                     for sprite in record.get("report", {}).get("sprites", [])) for manifest in (old, new)]
    lines += ["", (f"Files: {in_both - len(unchanged)} of the {in_both} in both changed, {len(added)} added, "
                   f"{len(removed)} removed")]
    if unchanged:
        lines.append(f"Unchanged ({len(unchanged)}): " + ", ".join(path.rsplit("/", 1)[-1].removesuffix(".redswf")
                                                                  for path in unchanged))
    lines.append(f"Stretched clips in all files: {stretched[0]} -> {stretched[1]}" if stretched[0] != stretched[1]
                 else f"Stretched clips in all files: {stretched[0]} in both")
    return lines


def describe_commit(manifest: dict) -> str:
    """The patcher commit a build was made from, for reports"""
    commit = manifest.get("patcher_commit")
    if commit is None:
        return "unknown"
    return commit[:9] + (" with uncommitted changes" if manifest.get("patcher_uncommitted_changes") else "")


def file_changes(old: dict, new: dict, compare_reports: bool) -> list[str]:
    """What changed in a file's record, as lines of text: a section for each aspect that changed (see
    FILE_ASPECTS), then one line naming those that didn't; none if nothing changed"""
    output_changed = _differs(old, new, "output_swf_sha256", "output_sha256")
    sections: dict[str, list[str]] = {aspect: [] for aspect in FILE_ASPECTS}
    if _differs(old, new, "source_swf_sha256", "source_sha256"):
        sections["source"].append("the game's version of the file changed")
    sections["frame rate"] += _frame_rate_changes(old, new, compare_reports)
    if not compare_reports:
        lines = _section("source", sections["source"]) + _section("frame rate", sections["frame rate"])
        return lines + (["Output changed (no details: the reports can't be compared)"] if output_changed else [])

    old_report, new_report = old["report"], new["report"]
    sections["settings"] += _settings_changes(old_report, new_report)
    sections["fixes"] += _fix_changes(old_report.get("fixes", []), new_report.get("fixes", []))
    old_warnings, new_warnings = ({warning["key"]: warning.get("hash") for warning in report["warnings"]}
                                  for report in (old_report, new_report))
    sections["warnings"] += _set_changes(set(old_warnings), set(new_warnings))
    sections["warnings"] += [f"{key}: its code changed" for key in sorted(old_warnings.keys() & new_warnings.keys())
                             if None not in (old_warnings[key], new_warnings[key]) and old_warnings[key] != new_warnings[key]]
    sections["unmet rules"] += _set_changes({format_unmet_rule(unmet) for unmet in old_report["unmet_rules"]},
                                            {format_unmet_rule(unmet) for unmet in new_report["unmet_rules"]})
    sections["code edits"] += _code_edit_changes(old_report, new_report)
    sections["clips"] += _sprite_changes(old_report.get("sprites", []), new_report.get("sprites", []))

    lines = [section_line for aspect, changes in sections.items() for section_line in _section(aspect, changes)]
    if not lines and not output_changed:
        return []
    if not lines:
        lines.append("Output changed, but the report is the same: the cause is in the patcher's code "
                     "(compare the patcher commits)")
    same = [aspect for aspect, changes in sections.items() if not changes]
    return lines + (["Same in both: " + ", ".join(same)] if same else [])


def _section(aspect: str, changes: list[str]) -> list[str]:
    """An aspect's changes under a heading; on the heading's line if there is only one"""
    if not changes:
        return []
    if len(changes) == 1:
        return [f"{aspect.capitalize()}: {changes[0]}"]
    return [f"{aspect.capitalize()}:", *(f"  {change}" for change in changes)]


def _differs(old: dict, new: dict, *keys) -> bool:
    """Do the records differ in the first of keys that both have?"""
    key = next((key for key in keys if key in old and key in new), None)
    return key is not None and old[key] != new[key]


def _frame_rate_changes(old: dict, new: dict, compare_reports: bool) -> list[str]:
    changes = [f"{old['fps']:g} -> {new['fps']:g} fps"] if old["fps"] != new["fps"] else []
    for key, name in (("ratio", "ratio"), ("holds", "holds per source frame")) if compare_reports else ():
        before, after = old["report"].get(key), new["report"].get(key)
        if before != after:
            changes.append(f"{name}: {before} -> {after}")
    return changes


def _settings_changes(old: dict, new: dict) -> list[str]:
    """Changes to the speed rules and include/exclude lists, and the clips each matched"""
    old_settings, new_settings = ({setting: matched for setting, matched in report_settings(report)
                                   if not setting.startswith("fix: ")} for report in (old, new))
    return ([f"removed: {setting}" for setting in old_settings if setting not in new_settings]
            + [f"added: {setting} (matches {matched or 'nothing'})"
               for setting, matched in new_settings.items() if setting not in old_settings]
            + [f"{setting}: matches {matched or 'nothing'} (was {old_settings[setting] or 'nothing'})"
               for setting, matched in new_settings.items()
               if setting in old_settings and matched != old_settings[setting]])


def _fix_changes(old_fixes: list, new_fixes: list) -> list[str]:
    old_by_name, new_by_name = ({fix["name"]: fix for fix in fixes} for fixes in (old_fixes, new_fixes))
    changes = [f"removed: {name}" for name in old_by_name if name not in new_by_name]
    for name, fix in new_by_name.items():
        before = old_by_name.get(name)
        if before is None:
            changes.append(f"added: {name}: {fix['result']}: {fix['detail']} (matches {fix['matched'] or 'nothing'})")
            continue
        if before["result"] != fix["result"]:
            changes.append(f"{name}: {before['result']} -> {fix['result']}")
        if before["detail"] != fix["detail"]:
            changes += [f"{name}: was: {before['detail']}", f"{name}: now: {fix['detail']}"]
        if before["matched"] != fix["matched"]:
            changes.append(f"{name}: matches {fix['matched'] or 'nothing'} (was {before['matched'] or 'nothing'})")
    return changes


def _code_edit_changes(old: dict, new: dict) -> list[str]:
    """Changes to the counts of code edits, and to the per-frame increments rescaled"""
    changes = []
    old_edits, new_edits = old.get("patched", {}), new.get("patched", {})
    if old_edits != new_edits:
        changes.append(", ".join(f"{kind}: {old_edits.get(kind, 0)} -> {new_edits.get(kind, 0)}"
                                 for kind in dict.fromkeys([*old_edits, *new_edits])
                                 if old_edits.get(kind, 0) != new_edits.get(kind, 0)))
    increment = "per-frame {property} += {old:g} -> {new:g} in {handler}".format_map
    return changes + _set_changes({increment(inc) for inc in old.get("increments", [])},
                                  {increment(inc) for inc in new.get("increments", [])})


def _set_changes(old: set, new: set) -> list[str]:
    return [f"gone: {item}" for item in sorted(old - new)] + [f"new: {item}" for item in sorted(new - old)]


def _sprite_changes(old_sprites: list, new_sprites: list) -> list[str]:
    """The clips whose handling changed, under a heading for each kind of change"""
    groups = defaultdict(list)  # heading -> clips
    pairs, gone, added = _match_sprites(old_sprites, new_sprites)
    for old, new in pairs:
        clip = _describe(new, old)
        if old["stretched"] != new["stretched"]:
            heading = "now stretched" if new["stretched"] else f"no longer stretched: {'; '.join(new['reasons'])}"
            groups[heading].append(clip)
        elif not new["stretched"]:
            if old["reasons"] != new["reasons"]:
                groups["still not stretched, for other reasons"].append(
                    f"{clip}: {'; '.join(old['reasons'])} -> {'; '.join(new['reasons'])}")
        else:
            if old["speed"] != new["speed"]:
                groups[f"speed: {old['speed']}x -> {new['speed']}x"].append(clip)
            old_tweens, new_tweens = old.get("interpolation"), new.get("interpolation")
            if bool(old_tweens) != bool(new_tweens):
                groups["interpolated now" if new_tweens else "no longer interpolated"].append(clip)
            elif new_tweens and (old_tweens["steps"], old_tweens["snapped"]) != (new_tweens["steps"], new_tweens["snapped"]):
                groups["interpolation changed (steps, snapped)"].append(
                    f"{clip}: {old_tweens['steps']}, {old_tweens['snapped']} -> {new_tweens['steps']}, {new_tweens['snapped']}")
    groups["clips gone"] += [_describe(sprite) for sprite in gone]
    groups["new clips"] += [f"{_describe(sprite)}{'' if sprite['stretched'] else ', not'} stretched" for sprite in added]

    lines = []
    for heading, clips in groups.items():
        if clips:
            lines += [f"{heading} ({len(clips)}):", *(f"  {clip}" for clip in clips)]
    return lines


def _describe(new: dict, old: dict | None = None) -> str:
    sprite_id = f"{old['id']} -> {new['id']}" if old and old["id"] != new["id"] else new["id"]
    frames = f"{old['frames']} -> {new['frames']}" if old and old["frames"] != new["frames"] else new["frames"]
    return f"{sprite_id} {new['class'] or '(no class)'} ({frames} frames)"


def _match_sprites(old_sprites: list, new_sprites: list) -> tuple[list, list, list]:
    """Pair up the clips of two versions of a file: by id where the class is the same, otherwise by
    class where only one clip on each side has it (ids shift when the game's file changes).
    Returns ([(old, new)], old clips without a pair, new clips without a pair)."""
    unpaired_old = {sprite["id"]: sprite for sprite in old_sprites}
    pairs, unpaired_new = [], []
    for new in new_sprites:
        old = unpaired_old.get(new["id"])
        if old is not None and old["class"] == new["class"]:
            pairs.append((unpaired_old.pop(new["id"]), new))
        else:
            unpaired_new.append(new)

    old_classes = Counter(sprite["class"] for sprite in unpaired_old.values())
    new_classes = Counter(sprite["class"] for sprite in unpaired_new)
    added = []
    for new in unpaired_new:
        if new["class"] and old_classes[new["class"]] == 1 and new_classes[new["class"]] == 1:
            old = next(sprite for sprite in unpaired_old.values() if sprite["class"] == new["class"])
            pairs.append((unpaired_old.pop(old["id"]), new))
        else:
            added.append(new)
    return pairs, list(unpaired_old.values()), added
