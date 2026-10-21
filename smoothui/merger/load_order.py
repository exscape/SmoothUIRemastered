"""The order in which the game loads the mods in Mods\\: for a file that several mods ship, the first mod in
the order wins. The rules, as Script Merger and the mod managers model them (checked in game):
- Only folders whose names start with "mod" are loaded. Mod managers disable a mod by renaming its folder
  (TW3MM and its forks put a ~ in front), or with Enabled=0 in its mods.settings section.
- Mods with a mods.settings section come before mods without one, whatever their names.
- Then a lower Priority comes first, if both mods have one; otherwise the names decide, lowercased and
  compared as UTF-16 code units.

mod.io mods always load before these, so they don't take part here (the scan only detects them).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from functools import cmp_to_key

from ..witcher3.mods_settings import ModSettings

# Folders in Mods\ that the game loads; anything else there is ignored (e.g. renamed to disable it)
MOD_FOLDER = re.compile(r"mod.+", re.IGNORECASE | re.DOTALL)

class Group(IntEnum):
    """Mods with a mods.settings section load before mods without one (disabled mods are left out)"""
    WITH_SECTION = 1
    WITHOUT_SECTION = 2


@dataclass(frozen=True, slots=True)
class Mod:
    folder: str                     # its folder in Mods\, as spelled there
    settings: ModSettings | None    # its section in mods.settings

    @property
    def enabled(self) -> bool:
        return self.settings is None or self.settings.enabled

    @property
    def group(self) -> Group:
        return Group.WITH_SECTION if self.settings is not None else Group.WITHOUT_SECTION

    @property
    def priority(self) -> int | None:
        return self.settings.priority if self.settings is not None else None

    @property
    def unordered(self) -> bool:
        """A section without a valid Priority: the game's comparer then isn't a total order (see
        undetermined_for)"""
        return self.settings is not None and self.priority is None


def is_mod_folder(name: str) -> bool:
    return MOD_FOLDER.fullmatch(name) is not None


def make_mods(folders, settings: dict[str, ModSettings]) -> list[Mod]:
    """A Mod for each mod folder among folders (names in Mods\\), with its section in settings (see
    mods_settings.read_mods_settings), in no particular order"""
    return [Mod(folder, settings.get(folder.lower())) for folder in folders if is_mod_folder(folder)]


def compare(a: Mod, b: Mod) -> int:
    """The game's comparer: the group, then the priorities if both have one and they differ, then the names
    (lowercased, compared as UTF-16 code units, as .NET compares strings)"""
    if a.group != b.group:
        return a.group - b.group
    if a.priority is not None and b.priority is not None and a.priority != b.priority:
        return a.priority - b.priority
    key_a, key_b = _name_key(a), _name_key(b)
    return (key_a > key_b) - (key_a < key_b)


def _name_key(mod: Mod) -> bytes:
    # Big-endian UTF-16 bytes compare in the order of their code units
    return mod.folder.lower().encode("utf-16-be", errors="surrogatepass")


def load_order(mods: list[Mod]) -> list[Mod]:
    """The enabled mods, in load order (the first one wins)"""
    return sorted((mod for mod in mods if mod.enabled), key=cmp_to_key(compare))


def undetermined_for(shippers: list[Mod]) -> list[Mod]:
    """The mods that make the winner for one file unknown, or [] if the order decides it. shippers are the
    mods that ship the file, in load order.

    A mod with a section but no valid Priority compares with every mod by name, while the others compare
    by priority among themselves, so the comparer can have cycles, and the order then depends on the game's
    sorting algorithm, which is unknown. So when such a mod ships the file along with another mod with a
    section, either may win. (A cycle through it could in theory reorder other mods too; not reported, as
    that is unlikely, and a wrong guess there only shows another mod's version of the screen.) Mods without
    a section always lose to those with one, so they never make a file's winner unknown."""
    with_section = [mod for mod in shippers if mod.group == Group.WITH_SECTION]
    if len(with_section) < 2 or not any(mod.unordered for mod in with_section):
        return []
    return with_section
