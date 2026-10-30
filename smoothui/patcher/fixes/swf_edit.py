"""What the fixes edit: an SWF's tag list, sprite by sprite, and its ActionScript, method by method"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple

from ..swf import MalformedSwfError, swf_get_header_offsets
from ..swf_patcher.abc_file import AbcFile, PoolAdditions, parse_abc, serialize_abc
from ..swf_patcher.swf_tags import (
    Tag,
    TagCode,
    build_sprite,
    parse_frame_label,
    parse_sprite_header,
    parse_sprite_tags,
    parse_symbol_class,
    parse_tags,
    serialize_tags,
    split_do_abc,
)

# When a fix runs (its module's PHASE; BEFORE_RESCALE if it has none): on the game's file, before the
# rescale, or on the rescaled file (or the game's, if it isn't rescaled), for a fix that must keep the
# in-between frames the rescale adds. Fixes that run before are covered by the rescale's verification.
BEFORE_RESCALE, AFTER_RESCALE = "before rescale", "after rescale"


class FixKind(Enum):
    """What a fix is, for users (a fix module's KIND). Its value is the header of a message about it
    missing, e.g. "Game bug fix missing: " + its CONSEQUENCE; a message may leave the header out."""
    GAME_BUG = "Game bug fix missing:"              # a bug in the game's own file
    SMOOTHUI_FIX = "Smooth UI fix missing:"         # a problem Smooth UI's rescale causes (e.g. by interpolating)
    SMOOTHUI_FEATURE = "Smooth UI feature missing:"  # something Smooth UI adds

    @property
    def header(self) -> str:
        return self.value


class CantApply(ValueError):
    """The file isn't as the fix expects (the game, or another mod, changed it), so it was left alone"""


class FixResult(NamedTuple):
    sprites: list[int]  # ids of the sprites the fix changed; empty if there was nothing to fix
    detail: str         # what it did, or why there was nothing to do


class AbcEdit:
    """One of the file's own DoABC tags, whose methods fixes replace (see SwfEdit.unit_of_class). Valid
    until SwfEdit.insert_abc, which moves the tags."""

    def __init__(self, swf: SwfEdit, tag_index: int):
        self._swf = swf
        self.tag_index = tag_index  # index in SwfEdit.tags

    @property
    def abc(self) -> AbcFile:
        return self._swf._own_abc(self.tag_index)

    def replace_method_code(self, method: int, code: bytes, exceptions: list):
        """Give a method (by its index in this ABC) new bytecode and exception table"""
        self.replace_methods({method: (code, exceptions)})

    def replace_methods(self, bodies: dict[int, tuple[bytes, list]], additions: PoolAdditions | None = None):
        """Give methods (by their index in this ABC) new bytecode and exception tables, with the
        constants their code names that the ABC didn't have (made with PoolAdditions(self.abc))"""
        swf, index = self._swf, self.tag_index
        old = swf.tags[index]
        prefix, _ = split_do_abc(old)
        swf.tags[index] = Tag(TagCode.DO_ABC, prefix + serialize_abc(self.abc, bodies, additions=additions),
                              old.long_header)
        del swf._parsed[index]
        swf.changed = True


class SwfEdit:
    """A decompressed SWF body (the data after the 8-byte header), whose sprites fixes replace"""

    def __init__(self, body: bytes):
        first_tag = swf_get_header_offsets(body)[1]
        if first_tag is None:
            raise MalformedSwfError("not a valid SWF body")
        self.header = bytes(body[:first_tag])
        self.tags = parse_tags(body, first_tag)
        self.changed = False
        self._added_abc = []        # DoABC tags added by insert_abc (not the file's own ActionScript)
        self._index_tags()

    def _index_tags(self):
        self._sprite_index = {}     # character id -> index in self.tags
        self._sprites_of_class = {}
        self._abc_indices = []      # indices in self.tags of the file's own DoABC tags
        self._parsed = {}           # index in self.tags -> the parsed ABC of that tag, once asked for
        for index, tag in enumerate(self.tags):
            if tag.code == TagCode.DEFINE_SPRITE:
                self._sprite_index[parse_sprite_header(tag)[0]] = index
            elif tag.code == TagCode.DO_ABC:
                if not any(tag is added for added in self._added_abc):
                    self._abc_indices.append(index)
            elif tag.code == TagCode.SYMBOL_CLASS:
                for character_id, class_name in parse_symbol_class(tag):
                    self._sprites_of_class.setdefault(class_name, []).append(character_id)

    def linked_sprite(self, class_name: str) -> tuple[int, list[Tag]]:
        """(character id, timeline) of the sprite linked to class_name; CantApply unless there is exactly one"""
        ids = [i for i in self._sprites_of_class.get(class_name, []) if i in self._sprite_index]
        if len(ids) != 1:
            raise CantApply(f"expected 1 sprite linked to {class_name}, found {len(ids)}")
        return ids[0], parse_sprite_tags(self.tags[self._sprite_index[ids[0]]])

    def linked_sprite_matching(self, class_pattern: str) -> tuple[int, str, list[Tag]]:
        """(character id, class name, timeline) of the sprite linked to a class whose whole name matches the
        regular expression class_pattern; CantApply unless exactly one sprite is linked to such a class.

        For classes that other mods rename, such as Flash's numbered library names (COINA_113 vs COINA_116)"""
        found = {i: name for name, ids in self._sprites_of_class.items() if re.fullmatch(class_pattern, name)
                 for i in ids if i in self._sprite_index}
        if len(found) != 1:
            raise CantApply(f"expected 1 sprite linked to a class matching {class_pattern}, found {len(found)}")
        (character_id, class_name), = found.items()
        return character_id, class_name, parse_sprite_tags(self.tags[self._sprite_index[character_id]])

    def sprite_tags(self, character_id: int) -> list[Tag] | None:
        """A sprite's timeline; None if there is no such sprite"""
        index = self._sprite_index.get(character_id)
        return None if index is None else parse_sprite_tags(self.tags[index])

    def class_of(self, character_id: int) -> str | None:
        return next((name for name, ids in self._sprites_of_class.items() if character_id in ids), None)

    def replace_sprite(self, character_id: int, tags: list[Tag], *, frames_removed: int = 0):
        """Give a sprite a new timeline, with frames_removed fewer frames than the old one"""
        index = self._sprite_index[character_id]
        old = self.tags[index]
        _, frame_count = parse_sprite_header(old)
        if sum(tag.code == TagCode.SHOW_FRAME for tag in tags) != sum(
                tag.code == TagCode.SHOW_FRAME for tag in parse_sprite_tags(old)) - frames_removed:
            raise AssertionError(f"BUG: a fix changed the frame count of sprite {character_id} unexpectedly")
        self.tags[index] = build_sprite(character_id, frame_count - frames_removed, tags, long_header=old.long_header)
        self.changed = True

    def _own_abc(self, index: int) -> AbcFile:
        if index not in self._parsed:
            self._parsed[index] = parse_abc(split_do_abc(self.tags[index])[1])
        return self._parsed[index]

    def class_names(self) -> set[str]:
        """The classes the file's own ActionScript defines, in all its DoABC tags"""
        return {abc.full_name_of(instance.name) for abc in map(self._own_abc, self._abc_indices)
                for instance in abc.instances}

    def unit_of_class(self, class_name: str) -> AbcEdit:
        """The DoABC tag of the file's own that defines class_name; CantApply unless exactly one does"""
        found = [index for index in self._abc_indices
                 if any(self._own_abc(index).full_name_of(instance.name) == class_name
                        for instance in self._own_abc(index).instances)]
        if not found:
            raise CantApply(f"found no class {class_name}")
        if len(found) > 1:
            raise CantApply(f"class {class_name} is defined in {len(found)} DoABC tags")
        return AbcEdit(self, found[0])

    def sprites_of_class(self, class_name: str) -> list[int]:
        """Ids of the sprites linked to class_name (none if there are none)"""
        return [i for i in self._sprites_of_class.get(class_name, []) if i in self._sprite_index]

    def insert_abc(self, tag: Tag):
        """Add a DoABC tag (classes of our own) right after the file's own, so that its classes are
        defined before anything runs. It is not one of the file's own tags (see unit_of_class)."""
        if tag.code != TagCode.DO_ABC or not self._abc_indices:
            raise CantApply("the file has no DoABC tag")
        self.tags.insert(self._abc_indices[-1] + 1, tag)
        self._added_abc.append(tag)
        self._index_tags()
        self.changed = True

    def body(self) -> bytes:
        return self.header + serialize_tags(self.tags)


def label_frames(tags: list[Tag]) -> dict[str, int]:
    """A timeline's frame labels -> their 0-based frames"""
    labels, frame = {}, 0
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            frame += 1
        elif tag.code == TagCode.FRAME_LABEL:
            labels[parse_frame_label(tag)] = frame
    return labels
