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
other than UTF-8 or UTF-16 with a BOM) is an error, never a guess.

set_entry writes a mod's entry as a text edit, so that everything else in the file stays as it was: other
sections, keys (e.g. Vortex's VK), comments, spelling, order and line endings. write_entry writes the file
as UTF-8 without a BOM, as at least some mod managers do, whatever it was read as.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from ..common import SmoothUIError, fail
from ..files import read_file, write_file

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


# ---- Writing an entry ----

# The line ending of a new file, as the game and the mod managers write it
NEW_FILE_NEWLINE = "\r\n"
# How keys we add are spelled
KEY_SPELLING = {"enabled": "Enabled", "priority": "Priority"}


def set_entry(text: str, name: str, enabled: bool = True, priority: int = 0) -> str:
    """text (a mods.settings file's content) with name's section set to enabled and priority.

    The game uses the first section for a mod, so that one is edited; later ones are left as they are.
    In it, Enabled and Priority keep their place and spelling, and only get a new value if theirs means
    something else (e.g. Enabled=true is left alone). A missing key is added on a new line right after the
    section's header, rather than at its end, where blank lines and comments usually belong to the
    next section. Without a section for name, one is appended, after a blank line. New lines use the
    file's line ending (its first one; CRLF if it has none)."""
    lines = text.splitlines(keepends=True)
    newline = next((_ending(line) for line in lines if _ending(line)), NEW_FILE_NEWLINE)
    values = {"enabled": "1" if enabled else "0", "priority": str(priority)}

    def means_wanted(key, value):
        if key == "enabled":
            return (value.strip() != "0") == enabled
        return PRIORITY.fullmatch(value.strip()) is not None and int(value) == priority

    start = next((i for i, line in enumerate(lines) if (_header_name(line) or "").lower() == name.lower()), None)
    if start is None:
        if not text:
            before = ""
        elif not _ending(text):
            before = newline * 2    # end the last line, then a blank line
        else:
            before = newline if lines[-1].strip() else ""
        return text + before + "".join(f"{line}{newline}" for line in
                                       (f"[{name}]", f"Enabled={values['enabled']}", f"Priority={values['priority']}"))

    end = next((i for i in range(start + 1, len(lines)) if _header_name(lines[i]) is not None), len(lines))
    missing = dict(values)
    for i in range(start + 1, end):
        before, equals, value = lines[i].partition("=")
        key = before.strip().lower()
        if not equals or key not in values:
            continue
        del missing[key]    # a key set twice in a section is an error, so it was set once
        ending = _ending(value)
        body = value[:len(value) - len(ending)]
        if not means_wanted(key, body):
            spaces = body[:len(body) - len(body.lstrip())]
            lines[i] = f"{before}={spaces}{values[key]}{ending}"
    if missing:
        if not _ending(lines[start]):
            lines[start] += newline
        lines[start + 1:start + 1] = [f"{KEY_SPELLING[key]}={value}{newline}" for key, value in missing.items()]
    return "".join(lines)


def _ending(line):
    """The line ending at the end of line, or "" """
    return line[len(line.rstrip("\r\n")):]


def _header_name(line):
    """The section name a line starts, or None"""
    header = SECTION.fullmatch(line.strip())
    return header[1] if header else None


def write_entry(path: Path, name: str, *, backup: Path | None = None) -> bool:
    """Give name an enabled section with Priority 0 in the mods.settings file at path (see set_entry),
    creating the file if there is none. Returns whether the file changed. If it did, and there was a
    file, its old content is written to backup first (replacing any file there). Written as UTF-8 without
    a BOM, as files.py writes files. Fails (SmoothUIError) if the file can't be read for certain or
    written (naming the backup, if one was written: the file may be broken then)."""
    data = read_file(path)
    if data is not None:
        parse_mods_settings(data)   # fails if it can't be read for certain
    old = _decode(data) if data is not None else ""
    text = set_entry(old, name)
    if text == old:
        return False
    backed_up = data is not None and backup is not None
    if backed_up:
        write_file(backup, data)
    try:
        write_file(path, text.encode("utf-8"))
        if parse_mods_settings(read_file(path) or b"").get(name.lower()) != ModSettings(True, 0):
            fail(f"mods.settings doesn't read back as written: [{name}] should have Enabled=1 and Priority=0")
    except SmoothUIError as e:
        if backed_up:
            fail(f"{e}. A copy of mods.settings from before is in {backup}")
        raise
    return True
