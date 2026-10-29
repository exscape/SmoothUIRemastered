"""Names for the screens Smooth UI patches, for the user: "Inventory" rather than
gameplay/gui_new/swf/inventory/panel_inventory.redswf. Full paths only go into the log.

A file not listed here (a newer Smooth UI release may patch more) is named by its file name. Light: the
check's messages use it.
"""

from __future__ import annotations

from collections import Counter

from ..common import profile_key

SWF_FOLDER = "gameplay/gui_new/swf/"

# By path under SWF_FOLDER, without .redswf; in the order the game shows them
NAMES = {
    "alchemy/panel_alchemy": "Alchemy",
    "character/panel_character": "Character, legacy skill tree",
    "character/panel_character_dupe": "Character, remastered skill tree",
    "common/panel_common": "Menu tabs and background",
    "glossary/panel_glossary_main": "Glossary",
    "glossary/panel_glossary_bestiary": "Glossary > Bestiary",
    "glossary/panel_glossary_tutorials": "Glossary > Tutorials",
    "glossary/panel_glossary_books": "Glossary > Books",
    "glossary/panel_glossary_encyclopedia": "Glossary > Characters",
    "crafting/panel_crafting": "Glossary > Crafting",
    "gwint/gwint_game": "Gwent",
    "gwint/deck_builder": "Gwent deck builder",
    "inventory/panel_inventory": "Inventory",
    "journal/panel_journal_quests": "Quests/journal",
    "noticeboard/panel_noticeboard": "Notice boards",
    "repair/panel_blacksmith": "Blacksmith",
    "repair/panel_enchanting": "Runewright/enchanting",
    "repair/panel_transmog": "Transmogrification",
    "worldmap/panel_worldmap": "Map",
}


# Groups of files that only work together (see build/profile.py's Group), by the name the profile gives them
GROUP_NAMES = {
    "hud": "HUD",
}
# Files in a group's folder are named after the group (by its name in GROUP_NAMES), then the file name less
# this prefix; the file named as the group itself is the group's name alone
GROUP_FOLDERS = {
    "hud/": ("hud", "hud_"),
}


def group_name(group: str) -> str:
    """A group's name for the user, e.g. HUD"""
    return GROUP_NAMES.get(group, group)


def file_name(path: str) -> str:
    """The file's name without its folder and .redswf, e.g. panel_inventory"""
    name = profile_key(path).rsplit("/", 1)[-1]
    return name.removesuffix(".redswf")


def screen_name(path: str) -> str:
    """The screen's name, e.g. Inventory (for a path as the profile spells it, in any case)"""
    key = profile_key(path).removeprefix(SWF_FOLDER).removesuffix(".redswf")
    if key in NAMES:
        return NAMES[key]
    for folder, (group, prefix) in GROUP_FOLDERS.items():
        if key.startswith(folder):
            rest = key.removeprefix(folder)
            return group_name(group) if rest == group else f"{group_name(group)}: {rest.removeprefix(prefix)}"
    return file_name(path)


def screen_label(path: str) -> str:
    """The screen's name and its file name, for headings, e.g. Inventory (panel_inventory)"""
    name = screen_name(path)
    return name if name == file_name(path) else f"{name} ({file_name(path)})"


def by_group(items: list, said) -> list[tuple[str, object]]:
    """items (each with a path, and a group or None) as (a label, the item), to list them: a group's items that
    say the same (said(item)) as one entry, e.g. ("HUD (25 other files)", the first of them), after the
    group's other items; each other item by its screen_label. A group is merged whole or not at all, so its
    files that are left out with it are one thing to tell, not one per file."""
    same = Counter((item.group, said(item)) for item in items if item.group)
    sizes = Counter(item.group for item in items if item.group)
    last = {item.group: index for index, item in enumerate(items) if item.group}
    listed, held = [], {}   # held: (group, what it says): its first item, until the group's last item is listed
    for index, item in enumerate(items):
        key = (item.group, said(item))
        if item.group and same[key] > 1:
            held.setdefault(key, item)
        else:
            listed.append((screen_label(item.path), item))
        if item.group and last[item.group] == index:
            for (group, _), first in [(key, first) for key, first in held.items() if key[0] == item.group]:
                count = same[group, said(first)]
                other = "other " if count < sizes[group] else ""
                listed.append((f"{group_name(group)} ({count} {other}files)", first))
    return listed
