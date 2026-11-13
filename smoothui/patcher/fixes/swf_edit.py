"""What the fixes edit: an SWF's tag list, sprite by sprite, and its ActionScript, method by method"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..swf import MalformedSwfError, swf_get_header_offsets
from ..swf_patcher.abc_bytecode import Instruction, branch_targets
from ..swf_patcher.abc_file import (
    AbcFile,
    InstanceInfo,
    PoolAdditions,
    parse_abc,
    serialize_abc,
)
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

# The tags that define a character, or (DefineFontInfo 13) add to a font's definition; its id comes first: SWF's
# DefineShape 2, DefineBits 6, DefineButton 7, DefineFont 10, DefineText 11, DefineFontInfo 13, DefineSound 14, DefineBitsLossless 20, DefineBitsJPEG2 21,
# DefineShape2 22, DefineShape3 32, DefineText2 33, DefineButton2 34, DefineBitsJPEG3 35, DefineBitsLossless2 36,
# DefineEditText 37, DefineSprite 39, DefineMorphShape 46, DefineFont2 48, DefineVideoStream 60, DefineFont3 75,
# DefineShape4 83, DefineMorphShape2 84, DefineBitsJPEG4 90 and DefineFont4 91, and Scaleform's
# DefineExternalImage 1001, DefineSubImage 1008 and DefineExternalImage2 1009
DEFINE_CODES = frozenset({2, 6, 7, 10, 11, 13, 14, 20, 21, 22, 32, 33, 34, 35, 36, 37, 39, 46, 48, 60, 75, 83, 84,
                          90, 91, 1001, 1008, 1009})
# DoABC flags: kDoAbcLazyInitializeFlag, as Flash sets it
DO_ABC_LAZY = 1


class Phase(Enum):
    """When a fix runs: on the game's file, before the rescale, or on the rescaled file (or the game's, if it
    isn't rescaled), for a fix that must keep the in-between frames the rescale adds. Fixes that run before are
    covered by the rescale's verification."""
    BEFORE_RESCALE = "before rescale"
    AFTER_RESCALE = "after rescale"


class FixKind(Enum):
    """What a fix is, for users (its Fix.kind). Its value is the header of a message about it missing, e.g.
    "Game bug fix missing: " + its consequence; a message may leave the header out."""
    GAME_BUG = "Game bug fix missing:"              # a bug in the game's own file
    SMOOTHUI_FIX = "Smooth UI fix missing:"         # a problem Smooth UI's rescale causes (e.g. by interpolating)
    SMOOTHUI_FEATURE = "Smooth UI feature missing:"  # something Smooth UI adds

    @property
    def header(self) -> str:
        return self.value


class CantApply(ValueError):
    """The file isn't as the fix expects (the game, or another mod, changed it), so it was left alone"""


@dataclass(frozen=True, slots=True)
class FixResult:
    sprites: list[int]  # ids of the sprites the fix changed; empty if there was nothing to fix
    detail: str         # what it did, or why there was nothing to do


@dataclass(frozen=True, slots=True)
class Fix:
    """One fix: each fix module defines one, as FIX (see fixes/__init__.py)"""
    name: str                                   # never renamed (see requires.FIX_NAMES)
    kind: FixKind
    consequence: str                            # what not having it means (see fixes/__init__.py)
    apply: Callable[[SwfEdit], FixResult]       # raises CantApply if the file isn't as the fix expects
    phase: Phase = Phase.BEFORE_RESCALE

    def __post_init__(self):
        if not self.consequence or self.consequence.endswith("."):
            raise ValueError(f"{self.name}: a fix's consequence is a sentence without a full stop")

    def missing_text(self) -> str:
        """The message about the fix missing, e.g. "Game bug fix missing: in Gwent, ... (gwent-rain-loop-fix)\""""
        return f"{self.kind.header} {self.consequence} ({self.name})"


class AbcEdit:
    """One of the file's own DoABC tags, whose methods fixes replace (see SwfEdit.unit_of_class). Valid
    until SwfEdit.insert_abc or insert_before_sprite, which move the tags."""

    def __init__(self, swf: SwfEdit, tag_index: int):
        self._swf = swf
        self.tag_index = tag_index  # index in SwfEdit.tags

    @property
    def abc(self) -> AbcFile:
        return self._swf._own_abc(self.tag_index)

    def instance(self, class_name: str) -> tuple[int, InstanceInfo]:
        """(index in abc.instances, instance info) of a class this ABC defines; CantApply if it has none"""
        abc = self.abc
        found = next(((index, instance) for index, instance in enumerate(abc.instances)
                      if abc.full_name_of(instance.name) == class_name), None)
        if found is None:
            raise CantApply(f"found no class {class_name}")
        return found

    def method(self, class_name: str, method_name: str) -> int:
        """The index of a method of a class this ABC defines, by name; CantApply if it has none with code"""
        abc = self.abc
        _, instance = self.instance(class_name)
        method = next((t.method for t in instance.traits
                       if t.method is not None and abc.name_of(t.name) == method_name), None)
        if method is None or method not in abc.bodies:
            raise CantApply(f"{class_name} has no method {method_name}")
        return method

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

    def __init__(self, body: bytes | bytearray):
        first_tag = swf_get_header_offsets(body)[1]
        if first_tag is None:
            raise MalformedSwfError("not a valid SWF body")
        self.header = bytes(body[:first_tag])
        self.tags = parse_tags(body, first_tag)
        self.changed = False
        self._added_abc = []        # DoABC tags added by insert_abc (not the file's own ActionScript)
        self._last_id = None        # the highest character id in use, once new_character_id is asked for one
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

    def sprite_ids(self) -> list[int]:
        """The ids of all sprites, in ascending order"""
        return sorted(self._sprite_index)

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

    def new_character_id(self) -> int:
        """An id for a character to add; CantApply if there is none left"""
        if self._last_id is None:
            self._last_id = max(struct.unpack_from("<H", tag.payload)[0] for tag in self.tags
                                if tag.code in DEFINE_CODES)
        if self._last_id == 0xFFFF:
            raise CantApply("the file has no character ids left")
        self._last_id += 1
        return self._last_id

    def insert_before_sprite(self, character_id: int, tags: list[Tag]):
        """Add tags (the definitions of characters for the sprite to place) right before a sprite"""
        index = self._sprite_index[character_id]
        self.tags[index:index] = tags
        self._index_tags()
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

    def sprites_of_class_or_subclasses(self, abc: AbcFile, class_name_index: int) -> list[int]:
        """Ids of the sprites linked to a class of abc (its multiname index) or to a class that extends it"""
        names = {class_name_index}
        grew = True
        while grew:
            grew = False
            for instance in abc.instances:
                if instance.super_name in names and instance.name not in names:
                    names.add(instance.name)
                    grew = True
        return sorted({sprite for name in names for sprite in self.sprites_of_class(abc.full_name_of(name))})

    def check_new_classes(self, abc: bytes):
        """CantApply if the file already defines a class that abc (an ABC file of our own classes) defines"""
        ours = parse_abc(abc)
        if clash := self.class_names() & {ours.full_name_of(instance.name) for instance in ours.instances}:
            raise CantApply(f"the file already defines {', '.join(sorted(clash))}")

    def insert_abc(self, package: str, abc: bytes):
        """Add a DoABC tag of our own classes (abc, an ABC file; the tag is named by their package, as Flash
        names it) right after the file's own, so that its classes are defined before anything runs. It is not
        one of the file's own tags (see unit_of_class)."""
        if not self._abc_indices:
            raise CantApply("the file has no DoABC tag")
        tag = Tag(TagCode.DO_ABC, struct.pack("<I", DO_ABC_LAZY) + package.encode() + b"\0" + abc, long_header=True)
        self.tags.insert(self._abc_indices[-1] + 1, tag)
        self._added_abc.append(tag)
        self._index_tags()
        self.changed = True

    def body(self) -> bytes:
        return self.header + serialize_tags(self.tags)


def read_abc(path: Path) -> bytes:
    """An ABC file of our own classes (see dev-tools/build-as3.py); CantApply if it is missing"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise CantApply(f"{path.name} is missing; build it with dev-tools/build-as3.py") from None


def replace_run(instructions: list[Instruction], first: int, last: int, code: bytes, where: str,
                what: str) -> dict[int, bytes]:
    """The edits (see abc_bytecode.assemble) that replace instructions[first:last + 1] with code; CantApply
    ("<where> has a branch into <what>") if a branch jumps into them past the first"""
    inner = {ins.offset for ins in instructions[first + 1:last + 1]}
    if inner & branch_targets(instructions):
        raise CantApply(f"{where} has a branch into {what}")
    return {first: code, **{i: b"" for i in range(first + 1, last + 1)}}


def label_frames(tags: list[Tag]) -> dict[str, int]:
    """A timeline's frame labels -> their 0-based frames"""
    labels, frame = {}, 0
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            frame += 1
        elif tag.code == TagCode.FRAME_LABEL:
            labels[parse_frame_label(tag)] = frame
    return labels
