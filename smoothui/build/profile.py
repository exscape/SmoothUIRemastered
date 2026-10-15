"""Profiles (which .redswf files to patch, and how) and compat override files.
The format is described at the top of config/profile_default.txt. Profiles don't set frame rates:
the build does (build-main.py --fps), and every file is patched to it.

Override files (build-compat.py --overrides) add settings for one other mod, on top of the ones the
Smooth UI release recorded in its manifest. They use the same format, but a file line refers to a
file both mods contain, rather than adding one. Speed rules are tried before the recorded ones, and
include/exclude/fix lines are added to them. Recorded settings can't be removed.

Override files may also mark patcher warnings that the other mod introduces as checked by hand, with
"reviewed: <kind> <Class.method> @<hash>" lines under "*", as the build prints them (see
smoothui/build/reviewed_warnings.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..common import fail, profile_key
from ..patcher.fixes import check_fix_name
from ..patcher.patcher_core import PatchSettings
from ..patcher.swf_patcher import (
    SpeedRule,
    SpriteRef,
    parse_speed,
    parse_sprite_pattern,
)
from .reviewed_warnings import check_review
from .shared import DEFAULT_PROFILE

SETTING = re.compile(r"(include|exclude|fix|reviewed|speed (\S+)):\s*(.*)")


@dataclass
class Entry:
    """The settings given for one file, or for "*" (every file)"""
    rules: list = field(default_factory=list)
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    fixes: list = field(default_factory=list)       # names (see patcher/fixes/); not under *
    reviewed: list = field(default_factory=list)    # reviews; override files only, under *

    def descriptions(self) -> list[str]:
        """Each speed/include/exclude/fix, as written in the profile"""
        return ([rule.description for rule in self.rules]
                + [f"include: {token}" for token in self.include]
                + [f"exclude: {token}" for token in self.exclude]
                + [f"fix: {name}" for name in self.fixes])


def _add_setting(entry: Entry, text: str, is_star: bool, is_override: bool):
    match = SETTING.fullmatch(text)
    if not match:
        raise ValueError(f"unknown setting '{text}'")
    key, speed, value = match.groups()
    if not value:
        raise ValueError(f"'{key}' needs a value")
    if key == "reviewed":
        if not (is_override and is_star):
            raise ValueError("reviewed: only belongs under * in compat override files "
                             "(main builds use config/reviewed_warnings.txt)")
        entry.reviewed.append(check_review(value))
        return
    if key == "fix":
        if is_star:
            raise ValueError("fix: can't be used under *, as each fix is for a specific file")
        check_fix_name(value)
        entry.fixes.append(value)
        return

    pattern = parse_sprite_pattern(value)
    if speed is not None:
        if isinstance(pattern, SpriteRef):
            raise ValueError("speed only takes class name regexes, not sprite ids")
        entry.rules.append(SpeedRule(pattern, parse_speed(speed)))
        return
    if is_star and isinstance(pattern, SpriteRef):
        raise ValueError("sprite ids can't be used under *, as they differ between files")
    getattr(entry, key).append(pattern)


def parse_profile(path: Path, *, is_override: bool = False) -> tuple[dict[str, Entry], Entry]:
    """Parse a profile, or a compat override file (whose file lines refer to files of the release).
    Returns ({profile key: Entry}, the "*" Entry)."""
    if not path.is_file():
        fail(f"file not found: {path}")
    files, star = {}, None
    entry = None        # what indented lines apply to

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].rstrip()
        if not line:
            continue
        try:
            if line[0].isspace():
                if entry is None:
                    raise ValueError("indented setting without a file (or *) above it")
                _add_setting(entry, line.strip(), entry is star, is_override)
            elif line == "*":
                if star is not None:
                    raise ValueError("* is given twice")
                entry = star = Entry()
            else:
                key = profile_key(line)
                if key in files:
                    raise ValueError(f"{key} is listed twice")
                entry = files[key] = Entry()
        except ValueError as e:
            fail(f"{path}:{number}: {e}")
    return files, star or Entry()


def _unique(items) -> tuple:
    return tuple(dict.fromkeys(items))


def load_profile(path, fps: int) -> tuple[dict[str, PatchSettings], list[str]]:
    """Read a profile (None: the default one), for a build at fps. Returns ({profile key:
    PatchSettings}, the "*" settings as written, for patcher_core.find_rule_issues)"""
    path = Path(path) if path else DEFAULT_PROFILE
    files, star = parse_profile(path)
    profile = {key: PatchSettings(fps, _unique(entry.rules + star.rules),
                                  _unique(entry.include + star.include), _unique(entry.exclude + star.exclude),
                                  _unique(entry.fixes))
               for key, entry in files.items()}
    print(f"Loaded {len(profile)} entries from {path}")
    return profile, star.descriptions()


def apply_overrides(profile: dict[str, PatchSettings], path) -> tuple[dict[str, PatchSettings], list[str], set[str]]:
    """Add the settings in a compat override file to profile (which holds only the files both mods
    contain). Returns (the new profile, the override file's "*" settings as written, the reviews it
    gives)."""
    path = Path(path)
    files, star = parse_profile(path, is_override=True)
    for key in files:
        if key not in profile:
            fail(f"{path}: {key} is not a file that both Smooth UI Remastered and the other mod contain")

    merged = {}
    for key, settings in profile.items():
        entry = files.get(key, Entry())
        merged[key] = PatchSettings(
            settings.target_fps,
            _unique(entry.rules + star.rules + list(settings.rules)),     # tried first
            _unique([*settings.include, *entry.include, *star.include]),
            _unique([*settings.exclude, *entry.exclude, *star.exclude]),
            _unique([*settings.fixes, *entry.fixes]))
    print(f"Applied overrides from {path} ({len(files)} files, {len(star.descriptions())} settings under *)")
    return merged, star.descriptions(), set(star.reviewed)
