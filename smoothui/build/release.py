"""Patching other mods' files as a Smooth UI release patched the game's: with the settings its manifest
records for each file (see shared.py's write_manifest).

Used by the compat build (build-compat.py) and the merger's package builder, to
build ONE compat patch, and a full compat package for all conflicts, respectively.
"""

import logging
from dataclasses import fields
from functools import partial
from typing import NamedTuple

from ..common import fail, profile_key
from ..mod_reviews import ModReviews
from ..patcher import patcher_core
from ..patcher.fixes import check_fix_name
from ..patcher.patcher_core import PatchSettings, RuleIssues
from ..patcher.swf_patcher import (
    SpeedRule,
    SpriteRef,
    parse_speed,
    parse_sprite_pattern,
)
from ..requires import fix_requirement, setting_requirement
from .reviewed_warnings import WarningReview, WarningSummary, review_of
from .shared import build_version, load_manifest, print_file_result

log = logging.getLogger(__name__)


def load_release_manifest(smooth_version, fps):
    """The manifest of one build of a Smooth UI release (smooth_version without a frame rate); fails
    unless it is one compat patches can be built from"""
    version = build_version(smooth_version, fps)
    manifest = load_manifest(version)
    too_old = (f"Smooth UI Remastered {smooth_version} is from before the 60 and 120 fps builds, which this "
               "patcher can't reproduce. Compat patches can be built for v0.3 and later releases")
    if manifest is None:
        fail(too_old if load_manifest(smooth_version) else f"no manifest exists for Smooth UI Remastered {version}")
    if "fps" not in manifest:
        fail(too_old)
    return manifest

def profile_from_manifest(manifest):
    """The settings each file was patched with in a Smooth UI release, from its manifest (see
    load_release_manifest): a profile ({profile key: PatchSettings}), so compat patches match the release."""
    profile = {}
    for key, record in manifest["files"].items():
        report = record["report"]
        profile[key] = PatchSettings(record["fps"],
                                     tuple(SpeedRule(rule["pattern"], parse_speed(rule["speed"])) for rule in report["rules"]),
                                     _recorded_tokens(report, "include"), _recorded_tokens(report, "exclude"),
                                     _recorded_fixes(key, report))
    return profile

def release_requires(settings) -> list[str]:
    """What the merger must support to patch as a release did (its manifest's "requires"; see
    requires.py), given the PatchSettings of its files"""
    # A new setting must have its requirement here, or older mergers would ignore it
    expected = {"target_fps", "rules", "include", "exclude", "fixes"}
    if (found := {field.name for field in fields(PatchSettings)}) != expected:
        fail(f"release_requires doesn't know the setting(s) {', '.join(sorted(found - expected))}: give them a "
             "requirement (see requires.py)")
    requires = set()
    for file_settings in settings:
        requires |= {setting_requirement(name) for name, used in
                     (("speed", file_settings.rules), ("include", file_settings.include), ("exclude", file_settings.exclude))
                     if used}
        requires |= {fix_requirement(name) for name in file_settings.fixes}
    return sorted(requires)

def _recorded_fixes(key, report):
    """The names of the fixes a release applied to a file"""
    names = tuple(fix["name"] for fix in report["fixes"])
    for name in names:
        try:
            check_fix_name(name)
        except ValueError as e:
            fail(f"{key}: the release's manifest names {e}; this version of the patcher can't reproduce it")
    return names

def _recorded_tokens(report, kind):
    """A report's include or exclude patterns, with sprite ids expecting the class and frame count
    recorded for them"""
    recorded_sprites = {sprite["id"]: (sprite["class"], sprite["frames"]) for sprite in report["sprites"]}
    parsed = (parse_sprite_pattern(setting["pattern"]) for setting in report[kind])
    return tuple(SpriteRef(token.id, recorded_sprites.get(token.id)) if isinstance(token, SpriteRef)
                 else token for token in parsed)


class ReleasePatch(NamedTuple):
    """What patch_as_release returns"""
    patched: list           # as patcher_core.patch_files returns them
    kept: list
    failed: list
    warnings: WarningSummary    # the warnings, less those the release had too (see patch_as_release)
    rule_issues: RuleIssues


def patch_as_release(inputs, profile, manifest, *, reviewed=(), mod_reviews: ModReviews | None = None, shared_settings=(),
                     on_file=print_file_result, pool=None) -> ReleasePatch:
    """Patch other mods' files (SourceFiles) with their settings in profile (usually profile_from_manifest's,
    for those files), and review the result against the release's manifest.

    The release's reports hold the warnings it had for each file; these were already there in the game's
    own version of the file, so they are hidden where the other mod's code is the same (see
    reviewed_warnings.py), along with the reviews in reviewed (for every file) and those mod_reviews has for
    the mod each file comes from (see mod_reviews.py). Settings that the release's
    reports say matched something, but no longer do, are warnings in rule_issues (see
    patcher_core.find_rule_issues; shared_settings are a profile's "*" block). on_file is as for
    patch_files, and also gets is_reviewed (see shared.print_file_result).

    Whether failures, rule issues or warnings are acceptable is up to the caller."""
    recorded = {key: record["report"] for key, record in manifest["files"].items()}
    per_file = {key: {review_of(warning) for warning in report["warnings"]} for key, report in recorded.items()}
    for source in inputs if mod_reviews else ():
        if found := mod_reviews.for_mod(source.origin):
            key = profile_key(source.path)
            per_file[key] = per_file.get(key, set()) | found
    review = WarningReview(reviewed, per_file)
    try:
        patched, kept, failed = patcher_core.patch_files(
            inputs, profile, on_file=partial(on_file, is_reviewed=review.is_reviewed), pool=pool)
    except ValueError as e:
        fail(str(e))
    warnings = review.summarize([*patched, *kept], unchecked=len(failed))
    rule_issues = patcher_core.find_rule_issues(patched, kept, shared_settings, recorded)
    return ReleasePatch(patched, kept, failed, warnings, rule_issues)
