"""Profiles (which .redswf files to patch, and how) and compat override files.
The format is described at the top of config/profile_default.txt.

Override files (build_compat --overrides) add settings for one other mod, on top of the ones the
Smooth UI release recorded in its manifest. They use the same format without [N] sections: a file
line refers to a file both mods contain, rather than adding one. fps replaces the recorded frame rate,
retime lines are tried before the recorded ones, and include/exclude lines are added to them.
Recorded settings can't be removed.

Override files may also mark patcher warnings that the other mod introduces as checked by hand, with
"reviewed: <warning key>" lines under "*" (see smoothui/reviewed_warnings.py)."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .build_common import DEFAULT_PROFILE, fail
from .common import profile_key
from .patcher.patcher_core import PatchSettings
from .patcher.swf_patcher import FpsRule, SpriteRef, parse_sprite_pattern

SETTING = re.compile(r"(fps|include|exclude|reviewed|retime (\S+)):\s*(.*)")
MIN_FPS, MAX_FPS = 1, 256


@dataclass
class Entry:
    """The settings given for one file, or for "*"; fps is None if not given"""
    fps: float | None = None
    rules: list = field(default_factory=list)
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    reviewed: list = field(default_factory=list)    # warning keys; override files only, under *

    def descriptions(self) -> list[str]:
        """Each retime/include/exclude, as written in the profile"""
        return ([rule.description for rule in self.rules]
                + [f"include: {token}" for token in self.include]
                + [f"exclude: {token}" for token in self.exclude])


def _parse_fps(text: str) -> float:
    try:
        fps = float(text)
    except ValueError:
        raise ValueError(f"invalid frame rate '{text}'") from None
    if not MIN_FPS <= fps <= MAX_FPS:
        raise ValueError(f"frame rate {text} is outside {MIN_FPS}-{MAX_FPS}")
    return fps


def _add_setting(entry: Entry, text: str, is_star: bool, is_override: bool):
    match = SETTING.fullmatch(text)
    if not match:
        raise ValueError(f"unknown setting '{text}'")
    key, retime_fps, value = match.groups()
    if not value:
        raise ValueError(f"'{key}' needs a value")
    if key == "fps":
        if is_star:
            raise ValueError("fps can't be set under *")
        if entry.fps is not None:
            raise ValueError("fps is given twice")
        entry.fps = _parse_fps(value)
        return
    if key == "reviewed":
        if not (is_override and is_star):
            raise ValueError("reviewed: only belongs under * in compat override files "
                             "(main builds use config/reviewed_warnings.txt)")
        entry.reviewed.append(value)
        return

    pattern = parse_sprite_pattern(value)
    if retime_fps is not None:
        if isinstance(pattern, SpriteRef):
            raise ValueError("retime only takes class name regexes, not sprite ids")
        entry.rules.append(FpsRule(pattern, _parse_fps(retime_fps)))
        return
    if is_star and isinstance(pattern, SpriteRef):
        raise ValueError("sprite ids can't be used under *, as they differ between files")
    getattr(entry, key).append(pattern)


def parse_profile(path: Path, *, sections: bool = True) -> tuple[dict[str, Entry], Entry]:
    """Parse a profile, or with sections=False, a compat override file (no [N] sections; its file
    lines only refer to files). Returns ({profile key: Entry}, the "*" Entry). In a profile, every
    file's Entry has its fps set."""
    if not path.is_file():
        fail(f"file not found: {path}")
    files, star = {}, None
    section_fps = {}    # profile key -> the frame rate of its [N] section
    current_fps = None
    entry = None        # what indented lines apply to

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].rstrip()
        if not line:
            continue
        try:
            if line[0].isspace():
                if entry is None:
                    raise ValueError("indented setting without a file (or *) above it")
                _add_setting(entry, line.strip(), entry is star, not sections)
            elif line.startswith("[") and line.endswith("]"):
                if not sections:
                    raise ValueError("[N] sections are not allowed here; use 'fps:' under a file")
                current_fps = _parse_fps(line[1:-1].strip())
                entry = None
            elif line == "*":
                if star is not None:
                    raise ValueError("* is given twice")
                entry = star = Entry()
            else:
                key = profile_key(line)
                if key in files:
                    raise ValueError(f"{key} is listed twice")
                if sections and current_fps is None:
                    raise ValueError("file listed before any [N] section")
                entry = files[key] = Entry()
                section_fps[key] = current_fps
        except ValueError as e:
            fail(f"{path}:{number}: {e}")

    if sections:
        for key, file_entry in files.items():
            if file_entry.fps is None:
                file_entry.fps = section_fps[key]
    return files, star or Entry()


def _unique(items) -> tuple:
    return tuple(dict.fromkeys(items))


def load_profile(path=None) -> tuple[dict[str, PatchSettings], list[str]]:
    """Read a profile. Returns ({profile key: PatchSettings}, the "*" settings as written, for
    patcher_core.find_rule_issues)"""
    path = Path(path) if path else DEFAULT_PROFILE
    files, star = parse_profile(path)
    profile = {key: PatchSettings(entry.fps, _unique(entry.rules + star.rules),
                                  _unique(entry.include + star.include), _unique(entry.exclude + star.exclude))
               for key, entry in files.items()}
    print(f"Loaded {len(profile)} entries from {path}")
    return profile, star.descriptions()


def apply_overrides(profile: dict[str, PatchSettings], path) -> tuple[dict[str, PatchSettings], list[str], set[str]]:
    """Add the settings in a compat override file to profile (which holds only the files both mods
    contain). Returns (the new profile, the override file's "*" settings as written, the warning
    keys it marks as reviewed)."""
    path = Path(path)
    files, star = parse_profile(path, sections=False)
    for key in files:
        if key not in profile:
            fail(f"{path}: {key} is not a file that both Smooth UI Remastered and the other mod contain")

    merged = {}
    for key, settings in profile.items():
        entry = files.get(key, Entry())
        merged[key] = PatchSettings(
            entry.fps or settings.target_fps,
            _unique(entry.rules + star.rules + list(settings.rules)),     # tried first
            _unique([*settings.include, *entry.include, *star.include]),
            _unique([*settings.exclude, *entry.exclude, *star.exclude]))
    print(f"Applied overrides from {path} ({len(files)} files, {len(star.descriptions())} settings under *)")
    return merged, star.descriptions(), set(star.reviewed)
