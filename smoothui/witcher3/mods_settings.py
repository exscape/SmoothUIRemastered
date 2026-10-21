"""Read <Documents>\\The Witcher 3\\mods.settings: which mods are enabled, and their priorities.

An INI file, one section per mod, named after its folder in Mods\\ (matched case-insensitively), with the
keys Enabled and Priority (key names case-insensitive):
- Enabled: the mod is disabled only if the value, stripped, is exactly 0.
- Priority: decimal digits, after stripping; anything else counts as no priority.
- Any other key (e.g. Vortex's VK) is ignored.
- A missing file is the same as an empty one.

A mod's section may appear more than once. The game uses the first one, so its values count. If a later one
sets a key the first doesn't, it isn't known which one counts, so that is an error.

A file that can't be read for certain (bad syntax, two sections whose names differ only in case, an encoding
other than UTF-8 or UTF-16 with a BOM) is an error, never a guess. This module only reads the file; writing
our own entry is done as a text edit, so that everything else in the file stays as it was.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from ..common import fail

PRIORITY = re.compile(r"[0-9]+")
SECTION = re.compile(r"\[(.+)\]")
# The keys the game reads; others are ignored
KEYS = ("enabled", "priority")


class ModSettings(NamedTuple):
    """A mod's section in mods.settings"""
    enabled: bool
    priority: int | None    # None if it has no valid Priority


def read_mods_settings(path: Path) -> dict[str, ModSettings]:
    """{section name, lowercased: ModSettings} for each section of the mods.settings file at path"""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as e:
        fail(f"can't read mods.settings: {e}")
    return parse_mods_settings(data)


def parse_mods_settings(data: bytes) -> dict[str, ModSettings]:
    """read_mods_settings for the file's content"""
    sections = {}   # name, lowercased: (name, the values of the first section by that name)
    for name, values in _sections(_decode(data)):
        if name.lower() not in sections:
            sections[name.lower()] = (name, values)
            continue
        first_name, first = sections[name.lower()]
        if name != first_name:
            fail(f"can't read mods.settings: [{first_name}] and [{name}] are sections for the same mod; "
                 "keep only one of them")
        if set(values) - set(first):
            fail(f"can't read mods.settings: [{name}] appears more than once, and a later one sets "
                 f"{', '.join(sorted(set(values) - set(first)))}, which the first one doesn't; keep only one of them")
    settings = {}
    for key, (_, values) in sections.items():
        priority = values.get("priority", "")
        settings[key] = ModSettings(enabled=values.get("enabled") != "0",
                                    priority=int(priority) if PRIORITY.fullmatch(priority) else None)
    return settings


def _sections(text):
    """(name, {key: value}) for each section in text, in order: the keys we read (lowercased), with their
    values stripped"""
    sections = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if header := SECTION.fullmatch(line):
            sections.append((header[1], {}))
            continue
        key, equals, value = line.partition("=")
        key = key.strip().lower()
        if not equals or not key:
            fail(f"can't read mods.settings: line {number} is neither a [section] nor a key=value line: {line}")
        if not sections:
            fail(f"can't read mods.settings: line {number} comes before the first [section]: {line}")
        name, values = sections[-1]
        if key in KEYS:
            if key in values:
                fail(f"can't read mods.settings: [{name}] sets {key} twice")
            values[key] = value.strip()
    return sections


def _decode(data: bytes) -> str:
    """mods.settings' text: UTF-16 with a BOM, or UTF-8 with or without one"""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        fail("can't read mods.settings: it is neither UTF-8 text nor UTF-16 text with a byte order mark")
