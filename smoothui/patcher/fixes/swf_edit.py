"""What the fixes edit: an SWF's tag list, sprite by sprite, and its ActionScript, method by method"""

from __future__ import annotations

from typing import NamedTuple

from ..swf import swf_get_header_offsets
from ..swf_patcher.abc_file import AbcFile, parse_abc, serialize_abc
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


class CantApply(ValueError):
    """The file isn't as the fix expects (the game, or another mod, changed it), so it was left alone"""


class FixResult(NamedTuple):
    sprites: list[int]  # ids of the sprites the fix changed; empty if there was nothing to fix
    detail: str         # what it did, or why there was nothing to do


class SwfEdit:
    """A decompressed SWF body (the data after the 8-byte header), whose sprites fixes replace"""

    def __init__(self, body: bytes):
        first_tag = swf_get_header_offsets(body)[1]
        if first_tag is None:
            raise ValueError("not a valid SWF body")
        self.header = bytes(body[:first_tag])
        self.tags = parse_tags(body, first_tag)
        self.changed = False
        self._sprite_index = {}     # character id -> index in self.tags
        self._sprites_of_class = {}
        self._abc_indices = []      # indices in self.tags of the DoABC tags
        self._abc = None            # the parsed ActionScript, once asked for
        for index, tag in enumerate(self.tags):
            if tag.code == TagCode.DEFINE_SPRITE:
                self._sprite_index[parse_sprite_header(tag)[0]] = index
            elif tag.code == TagCode.DO_ABC:
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

    @property
    def abc(self) -> AbcFile:
        """The file's ActionScript; CantApply unless it has exactly one DoABC tag"""
        if len(self._abc_indices) != 1:
            raise CantApply(f"expected 1 DoABC tag, found {len(self._abc_indices)}")
        if self._abc is None:
            self._abc = parse_abc(split_do_abc(self.tags[self._abc_indices[0]])[1])
        return self._abc

    def sprites_of_class(self, class_name: str) -> list[int]:
        """Ids of the sprites linked to class_name (none if there are none)"""
        return [i for i in self._sprites_of_class.get(class_name, []) if i in self._sprite_index]

    def replace_method_code(self, method: int, code: bytes, exceptions: list):
        """Give a method (by its index in the ABC file) new bytecode and exception table"""
        index = self._abc_indices[0]
        old = self.tags[index]
        prefix, _ = split_do_abc(old)
        self.tags[index] = Tag(TagCode.DO_ABC, prefix + serialize_abc(self.abc, {method: (code, exceptions)}),
                               old.long_header)
        self._abc = None
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
