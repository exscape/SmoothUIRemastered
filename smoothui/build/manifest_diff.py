"""Compare two manifests (see shared.write_manifest), to see what a build changed: files added or
removed, and per file the clips whose handling changed (stretched or not, frame rate,
interpolation), along with the settings, warnings and code edits behind that. Only for information.

build-main.py --write-manifest compares the new manifest with the previous version's;
manifest-diff.py compares any two.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from ..common import fail, normalize_version, print_step_header
from ..patcher.swf_patcher import format_unmet_rule, report_settings
from .shared import MANIFEST_DIR, load_manifest

VERSION = re.compile(r"v(\d+(?:\.\d+)*)([a-z][a-z0-9]*)?")
# Versions with the same number, from least to most recent; other suffixes come before these
SUFFIXES = ("beta", "dev", "")


def version_key(version: str):
    """Sort key for versions like v0.3beta; None if it isn't one. Numbers compare as decimals
    (0.31 comes after 0.3, and before 0.4), then by suffix (see SUFFIXES)."""
    match = VERSION.fullmatch(normalize_version(version))
    if not match:
        return None
    first, *rest = match.group(1).split(".")
    suffix = match.group(2) or ""
    return (int(first), *(Decimal("0." + part) for part in rest)), SUFFIXES.index(suffix) if suffix in SUFFIXES else -1


def previous_manifest(version: str) -> Path | None:
    """The manifest of the most recent version before this one (never this version's own)"""
    key = version_key(version)
    older = [(other, path) for path in MANIFEST_DIR.glob("*.json")
             if (other := version_key(path.stem)) is not None and key is not None and other < key]
    return max(older)[1] if older else None


def print_changes_since_previous(version_or_path):
    """Print what changed in a manifest (a version, or a path) since the previous version's"""
    version = Path(version_or_path).stem if str(version_or_path).endswith(".json") else version_or_path
    previous = previous_manifest(version)
    if previous is None:
        print(f"No manifest of a version before {normalize_version(version)} to compare with")
        return
    print_manifest_diff(previous, version_or_path)


def print_manifest_diff(old, new):
    """Print what changed from manifest old to new (versions, or paths)"""
    manifests = []
    for version_or_path in (old, new):
        manifest = load_manifest(version_or_path)
        if manifest is None:
            fail(f"manifest not found: {version_or_path}")
        manifests.append(manifest)
    print_step_header(f"Changes from {manifests[0]['created_mod_version']} to {manifests[1]['created_mod_version']}")
    print("\n".join(diff_manifests(*manifests)), flush=True)


def diff_manifests(old: dict, new: dict) -> list[str]:
    """What changed from manifest old to new, as lines of text"""
    lines = []
    if old.get("targeted_game_version") != new.get("targeted_game_version"):
        lines.append(f"Game version: {old.get('targeted_game_version')} -> {new.get('targeted_game_version')}")
    old_files, new_files = old["files"], new["files"]
    lines += [f"Removed: {path}" for path in sorted(old_files.keys() - new_files.keys())]
    lines += [f"Added: {path} ({new_files[path]['fps']:g} fps)" for path in sorted(new_files.keys() - old_files.keys())]

    changed = 0
    for path in sorted(old_files.keys() & new_files.keys()):
        changes = _file_changes(old_files[path], new_files[path])
        if changes:
            changed += 1
            lines += ["", path, *("  " + change for change in changes)]

    stretched = [sum(sprite["stretched"] for record in manifest["files"].values()
                     for sprite in record.get("report", {}).get("sprites", [])) for manifest in (old, new)]
    lines += ["", f"{changed} of {len(old_files.keys() & new_files.keys())} files in both changed; "
                  f"stretched clips in all files: {stretched[0]} -> {stretched[1]}"]
    return lines


def _file_changes(old: dict, new: dict) -> list[str]:
    changes = []
    if _differs(old, new, "source_swf_sha256", "source_sha256"):
        changes.append("the game's version of the file changed")
    if old["fps"] != new["fps"]:
        changes.append(f"frame rate: {old['fps']:g} -> {new['fps']:g} fps")
    if "report" in old and "report" in new:
        changes += _report_changes(old["report"], new["report"])
    elif _differs(old, new, "output_swf_sha256", "output_sha256"):
        changes.append("output changed (a manifest has no report, so there are no details)")
    if not changes and _differs(old, new, "output_swf_sha256", "output_sha256"):
        changes.append("output changed, with the same settings and clip handling")
    return changes


def _differs(old: dict, new: dict, *keys) -> bool:
    """Do the records differ in the first of keys that both have?"""
    key = next((key for key in keys if key in old and key in new), None)
    return key is not None and old[key] != new[key]


def _report_changes(old: dict, new: dict) -> list[str]:
    changes = []
    old_settings, new_settings = dict(report_settings(old)), dict(report_settings(new))
    changes += [f"setting removed: {setting}" for setting in old_settings if setting not in new_settings]
    changes += [f"setting added: {setting} (matches {matched or 'nothing'})"
                for setting, matched in new_settings.items() if setting not in old_settings]
    changes += [f"{setting}: matches {matched or 'nothing'} (was {old_settings[setting] or 'nothing'})"
                for setting, matched in new_settings.items() if setting in old_settings and matched != old_settings[setting]]

    changes += _set_changes("unmet rule", {format_unmet_rule(unmet) for unmet in old["unmet_rules"]},
                            {format_unmet_rule(unmet) for unmet in new["unmet_rules"]})
    old_warnings, new_warnings = ({warning["key"]: warning.get("hash") for warning in report["warnings"]}
                                  for report in (old, new))
    changes += _set_changes("warning", set(old_warnings), set(new_warnings))
    changes += [f"warning {key}: its code changed" for key in sorted(old_warnings.keys() & new_warnings.keys())
                if None not in (old_warnings[key], new_warnings[key]) and old_warnings[key] != new_warnings[key]]

    old_edits, new_edits = old.get("patched", {}), new.get("patched", {})
    if old_edits != new_edits:
        changes.append("code edits: " + ", ".join(f"{kind} {old_edits.get(kind, 0)} -> {count}"
                                                  for kind, count in new_edits.items() if old_edits.get(kind, 0) != count))
    increment = "per-frame {property} += {old:g} -> {new:g} in {handler}".format_map
    changes += _set_changes("increment", {increment(inc) for inc in old.get("increments", [])},
                            {increment(inc) for inc in new.get("increments", [])})
    return changes + _sprite_changes(old.get("sprites", []), new.get("sprites", []))


def _set_changes(what: str, old: set, new: set) -> list[str]:
    return [f"{what} gone: {item}" for item in sorted(old - new)] + [f"new {what}: {item}" for item in sorted(new - old)]


def _sprite_changes(old_sprites: list, new_sprites: list) -> list[str]:
    """The clips whose handling changed, under a heading for each kind of change"""
    groups = defaultdict(list)  # heading -> clips
    pairs, gone, added = _match_sprites(old_sprites, new_sprites)
    for old, new in pairs:
        clip = _describe(new, old)
        if old["stretched"] != new["stretched"]:
            heading = "now stretched" if new["stretched"] else f"no longer stretched: {'; '.join(new['reasons'])}"
            groups[heading].append(clip)
        elif new["stretched"]:
            if old["fps"] != new["fps"]:
                groups[f"retimed: {old['fps']:g} -> {new['fps']:g} fps"].append(clip)
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Show what changed between two Smooth UI Remastered manifests")
    ap.add_argument("manifests", nargs="+", metavar="MANIFEST",
                    help="versions (e.g. v0.3, looked up in manifests/) or paths to manifest files: OLD NEW, "
                         "or only NEW to compare it with the previous version's")
    args = ap.parse_args(argv)
    if len(args.manifests) > 2:
        ap.error("give one or two manifests")
    if len(args.manifests) == 1:
        print_changes_since_previous(args.manifests[0])
    else:
        print_manifest_diff(*args.manifests)
