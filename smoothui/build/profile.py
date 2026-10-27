"""Profiles (which .redswf files to patch, and how) and compat override files.
The format is described at the top of config/profile_default.txt. Profiles don't set frame rates:
the build does (build-main.py --fps), and every file is patched to it.

Override files (build-compat.py --overrides) add settings for one other mod, on top of the ones the
Smooth UI release recorded in its manifest. They use the same format, but a file line refers to a
file both mods contain, rather than adding one. Speed rules are tried before the recorded ones, and
include/exclude/fix lines are added to them. Recorded settings can't be removed.

Override files may also mark patcher warnings that the other mod introduces as checked by hand, with
"reviewed: <kind> <Class.method> @<hash>" lines under "*", as the build prints them (see
smoothui/build/reviewed_warnings.py). They can't have groups."""

from __future__ import annotations

import logging
import re
from collections import Counter
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

log = logging.getLogger(__name__)

SETTING = re.compile(r"(include|exclude|fix|reviewed|source fps|shipped|speed (\S+)):\s*(.*)")
# Where a setting is: under a file, under "*", or directly under a group (for every file of the group)
FILE, STAR, GROUP = "file", "*", "group"


@dataclass
class Entry:
    """The settings given for one file, for "*" (every file), or for every file of a group"""
    rules: list = field(default_factory=list)
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    fixes: list = field(default_factory=list)       # names (see patcher/fixes/); only under a file
    reviewed: list = field(default_factory=list)    # reviews; override files only, under *
    source_fps: float | None = None                 # not under *
    group: str | None = None                        # a file's group (see Group)

    def descriptions(self) -> list[str]:
        """Each speed/include/exclude/fix, as written in the profile"""
        return ([rule.description for rule in self.rules]
                + [f"include: {token}" for token in self.include]
                + [f"exclude: {token}" for token in self.exclude]
                + [f"fix: {name}" for name in self.fixes])


@dataclass
class Group:
    """A group block: files that only work together, so they are patched all or none (the merger leaves
    out the whole group if one can't be), with settings for every one of them"""
    name: str
    settings: Entry = field(default_factory=Entry)
    shipped: bool = True        # "shipped: no": the release records the files but doesn't pack them, so
                                # only the merger builds them (from the game's files)


def _add_setting(entry: Entry, text: str, where: str, is_override: bool):
    match = SETTING.fullmatch(text)
    if not match:
        raise ValueError(f"unknown setting '{text}'")
    key, speed, value = match.groups()
    if not value:
        raise ValueError(f"'{key}' needs a value")
    if key == "reviewed":
        if not (is_override and where == STAR):
            raise ValueError("reviewed: only belongs under * in compat override files "
                             "(main builds use config/reviewed_warnings.txt)")
        entry.reviewed.append(check_review(value))
        return
    if key == "shipped":
        raise ValueError("shipped: only belongs directly under a group")
    if key == "fix":
        if where != FILE:
            raise ValueError("fix: only belongs under a file, as each fix is for a specific file")
        check_fix_name(value)
        entry.fixes.append(value)
        return
    if key == "source fps":
        if where == STAR or is_override:
            raise ValueError("source fps: only belongs under a file or a group in a profile")
        if entry.source_fps is not None:
            raise ValueError("source fps: is given twice")
        try:
            entry.source_fps = float(value)
        except ValueError:
            raise ValueError(f"source fps: {value!r} is not a number") from None
        if not 1 <= entry.source_fps <= 255:
            raise ValueError(f"source fps: {value} is outside 1-255")
        return

    pattern = parse_sprite_pattern(value)
    if speed is not None:
        if isinstance(pattern, SpriteRef):
            raise ValueError("speed only takes class name regexes, not sprite ids")
        entry.rules.append(SpeedRule(pattern, parse_speed(speed)))
        return
    if where != FILE and isinstance(pattern, SpriteRef):
        raise ValueError(f"sprite ids can't be used under {'*' if where == STAR else 'a group'}, as they differ "
                         "between files")
    getattr(entry, key).append(pattern)


def _add_group_setting(group: Group, text: str):
    if text.startswith("shipped:"):
        if text.split(":", 1)[1].strip() != "no":
            raise ValueError("shipped: only takes no (files are shipped unless a group says otherwise)")
        group.shipped = False
        return
    _add_setting(group.settings, text, GROUP, False)


def parse_profile(path: Path, *, is_override: bool = False) -> tuple[dict[str, Entry], Entry, dict[str, Group]]:
    """Parse a profile, or a compat override file (whose file lines refer to files of the release).
    Returns ({profile key: Entry}, the "*" Entry, {name: Group}).

    Under a "group: NAME" line, the lines indented one level are the group's settings (for every file of
    the group) and its files, told apart by whether they parse as a setting; a file's own settings are
    indented deeper."""
    if not path.is_file():
        fail(f"file not found: {path}")
    files, star, groups = {}, None, {}
    entry = None        # what indented lines apply to (outside groups); the file whose settings follow (in one)
    group = None        # the group block the line is in
    group_indent = None     # the indentation of its settings and files

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].rstrip()
        if not line:
            continue
        indent, text = len(line) - len(line.lstrip()), line.strip()
        try:
            if indent and group is not None:
                group_indent = group_indent or indent
                if indent == group_indent and SETTING.fullmatch(text):
                    _add_group_setting(group, text)
                    entry = None
                elif indent == group_indent:
                    entry = _add_file(files, text, group.name)
                elif indent > group_indent:
                    if entry is None:
                        raise ValueError("setting indented deeper, without a file of the group above it")
                    _add_setting(entry, text, FILE, is_override)
                else:
                    raise ValueError("indented less than the group's other lines")
            elif indent:
                if entry is None:
                    raise ValueError("indented setting without a file (or *) above it")
                _add_setting(entry, text, STAR if entry is star else FILE, is_override)
            elif text == "*":
                if star is not None:
                    raise ValueError("* is given twice")
                entry, group = (star := Entry()), None
            elif text.startswith("group:"):
                if is_override:
                    raise ValueError("groups belong in profiles, not in compat override files")
                name = text.split(":", 1)[1].strip()
                if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
                    raise ValueError(f"group: {name!r} isn't a valid name (lowercase letters, digits, - and _)")
                if name in groups:
                    raise ValueError(f"group {name} is given twice")
                entry, group, group_indent = None, Group(name), None
                groups[name] = group
            else:
                entry, group = _add_file(files, text, None), None
        except ValueError as e:
            fail(f"{path}:{number}: {e}")
    for name, found in groups.items():
        if not any(entry.group == name for entry in files.values()):
            fail(f"{path}: group {name} has no files")
    return files, star or Entry(), groups


def _add_file(files: dict[str, Entry], text: str, group: str | None) -> Entry:
    key = profile_key(text)
    if key in files:
        raise ValueError(f"{key} is listed twice")
    files[key] = Entry(group=group)
    return files[key]


def _unique(items) -> tuple:
    return tuple(dict.fromkeys(items))


def load_profile(path, fps: int) -> tuple[dict[str, PatchSettings], list[str], dict[str, Group]]:
    """Read a profile (None: the default one), for a build at fps. Returns ({profile key:
    PatchSettings}, the settings shared by several files as written (under "*" and the groups), for
    patcher_core.find_rule_issues, {profile key: its Group} for the files in one)"""
    path = Path(path) if path else DEFAULT_PROFILE
    files, star, groups = parse_profile(path)
    profile, group_of = {}, {}
    for key, entry in files.items():
        group = groups.get(entry.group)
        shared = group.settings if group else Entry()
        if group:
            group_of[key] = group
        profile[key] = PatchSettings(fps, _unique(entry.rules + shared.rules + star.rules),
                                     _unique(entry.include + shared.include + star.include),
                                     _unique(entry.exclude + shared.exclude + star.exclude),
                                     _unique(entry.fixes),
                                     entry.source_fps if entry.source_fps is not None else shared.source_fps)
    counts = Counter(entry.group for entry in files.values())
    log.info(f"Loaded {len(profile)} entries from {path}"
             + "".join(f"; group {name}: {counts[name]} files"
                       + ("" if group.shipped else ", not shipped") for name, group in groups.items()))
    shared = _unique([*star.descriptions(), *(text for group in groups.values() for text in group.settings.descriptions())])
    return profile, list(shared), group_of


def apply_overrides(profile: dict[str, PatchSettings], path) -> tuple[dict[str, PatchSettings], list[str], set[str]]:
    """Add the settings in a compat override file to profile (which holds only the files both mods
    contain). Returns (the new profile, the override file's "*" settings as written, the reviews it
    gives)."""
    path = Path(path)
    files, star, _ = parse_profile(path, is_override=True)
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
            _unique([*settings.fixes, *entry.fixes]),
            settings.source_fps)
    log.info(f"Applied overrides from {path} ({len(files)} files, {len(star.descriptions())} settings under *)")
    return merged, star.descriptions(), set(star.reviewed)
