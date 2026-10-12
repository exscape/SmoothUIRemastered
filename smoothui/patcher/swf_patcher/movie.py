"""The parsed SWF body: timelines, linkage classes and ActionScript classes, as the planner needs them"""

from __future__ import annotations

import struct
from collections import defaultdict
from dataclasses import dataclass

from ...swf import FPS_FIXED_POINT_SCALE, swf_get_header_offsets
from ...swf_tags import (
    Tag,
    TagCode,
    parse_frame_label,
    parse_sprite_header,
    parse_sprite_tags,
    parse_symbol_class,
    parse_tags,
    split_do_abc,
)
from .abc_bytecode import Instruction, disassemble
from .abc_file import (
    CONSTANT_INT,
    CONSTANT_UINT,
    CONSTANT_UTF8,
    METHOD_TRAIT_KINDS,
    VALUE_TRAIT_KINDS,
    parse_abc,
)
from .place_object import PLACE_OBJECT_CODES, parse_place_object

ROOT_TIMELINE = 0   # stands in for the main timeline's id where a sprite id is expected


class RescaleError(ValueError):
    """The SWF can't be handled, or the rescaled result failed verification"""


@dataclass
class Sprite:
    character_id: int
    tag_index: int              # index of its DefineSprite in SwfMovie.tags
    tags: list[Tag]             # the sprite's own timeline
    frame_count: int            # number of ShowFrame tags
    declared_frame_count: int   # frame count in the DefineSprite header
    labels: dict[int, str]      # 0-based frame -> label


class SwfMovie:
    def __init__(self, body: bytes):
        self.body = bytes(body)
        self.fps_offset, self.first_tag_offset = swf_get_header_offsets(self.body)
        if self.fps_offset is None:
            raise RescaleError("not a valid SWF body")
        self.fps = struct.unpack_from("<H", self.body, self.fps_offset)[0] / FPS_FIXED_POINT_SCALE
        self.tags = parse_tags(self.body, self.first_tag_offset)

        self._load_timelines()
        self._load_symbol_classes()
        self._load_abc()
        self._descendants_cache = {}

    # --- Timelines ---

    def _load_timelines(self):
        self.sprites: dict[int, Sprite] = {}
        # parent sprite id (ROOT_TIMELINE for the main timeline) -> {(child character id, instance name or None)}
        self.placements = defaultdict(set)

        for index, tag in enumerate(self.tags):
            if tag.code != TagCode.DEFINE_SPRITE:
                continue
            character_id, declared_frames = parse_sprite_header(tag)
            sprite_tags = parse_sprite_tags(tag)
            labels, frame = {}, 0
            for sub in sprite_tags:
                if sub.code == TagCode.SHOW_FRAME:
                    frame += 1
                elif sub.code == TagCode.FRAME_LABEL:
                    labels[frame] = parse_frame_label(sub)
            self.sprites[character_id] = Sprite(character_id, index, sprite_tags, frame, declared_frames, labels)
            self._record_placements(sprite_tags, character_id)
        self._record_placements(self.tags, ROOT_TIMELINE)

    def _record_placements(self, tags, parent):
        for tag in tags:
            if tag.code not in PLACE_OBJECT_CODES:
                continue
            place = parse_place_object(tag)
            if place.character_id is None:
                continue
            self.placements[parent].add((place.character_id, place.name))

    def multi_frame_sprites(self):
        return [sprite for sprite in self.sprites.values() if sprite.frame_count > 1]

    # --- Linkage (SymbolClass) ---

    def _load_symbol_classes(self):
        self.class_of_sprite: dict[int, str] = {}
        self.sprites_of_class = defaultdict(list)   # linkage class name -> [character ids]
        for tag in self.tags:
            if tag.code == TagCode.SYMBOL_CLASS:
                for character_id, class_name in parse_symbol_class(tag):
                    self.class_of_sprite[character_id] = class_name
                    self.sprites_of_class[class_name].append(character_id)

    def class_name(self, character_id: int) -> str:
        return self.class_of_sprite.get(character_id, "")

    # --- ActionScript ---

    def _load_abc(self):
        abc_indices = [i for i, tag in enumerate(self.tags) if tag.code == TagCode.DO_ABC]
        if len(abc_indices) != 1:
            raise RescaleError(f"expected exactly one DoABC tag, found {len(abc_indices)}")
        self.abc_tag_index = abc_indices[0]
        self.abc_prefix, abc_bytes = split_do_abc(self.tags[self.abc_tag_index])
        abc = self.abc = parse_abc(abc_bytes)

        self.method_owner = {}      # method index -> "pkg.Class.method"
        self.class_of_method = {}   # method index -> "pkg.Class"
        self.superclass = {}        # "pkg.Class" -> "pkg.Super" or None

        for instance, cls in zip(abc.instances, abc.classes):
            class_name = abc.full_name_of(instance.name)
            self.superclass[class_name] = abc.full_name_of(instance.super_name) if instance.super_name else None
            self._set_method_owner(instance.constructor, class_name, "<iinit>")
            self._set_method_owner(cls.static_init, class_name, "<cinit>")
            for trait in instance.traits + cls.traits:
                if trait.kind in METHOD_TRAIT_KINDS:
                    self._set_method_owner(trait.method, class_name, abc.name_of(trait.name))

        self._load_string_typing()
        self.code: dict[int, list[Instruction]] = {m: disassemble(body.code) for m, body in abc.bodies.items()}

    def _set_method_owner(self, method, class_name, method_name):
        self.method_owner[method] = f"{class_name}.{method_name}"
        self.class_of_method[method] = class_name

    def _load_string_typing(self):
        """Find the property names that always hold a String, and the methods that return one, so
        label (string) arguments can be told apart from frame numbers"""
        abc = self.abc
        property_types = defaultdict(set)
        self.string_methods = set()
        for instance, cls in zip(abc.instances, abc.classes):
            for trait in instance.traits + cls.traits:
                if trait.kind in VALUE_TRAIT_KINDS:
                    type_name = abc.name_of(trait.type_name) if trait.type_name else "*"
                    if trait.value_kind == CONSTANT_UTF8:
                        type_name = "String"
                    elif trait.value_kind in (CONSTANT_INT, CONSTANT_UINT):
                        type_name = "int"
                    property_types[abc.name_of(trait.name)].add(type_name)
                elif trait.kind in METHOD_TRAIT_KINDS:
                    return_type = abc.methods[trait.method].return_type
                    if return_type and abc.name_of(return_type) == "String":
                        self.string_methods.add(abc.name_of(trait.name))
        self.string_properties = {name for name, types in property_types.items() if types == {"String"}}

    def owner_name(self, method: int, default: str = "?") -> str:
        return self.method_owner.get(method, default)

    def subclasses_of(self, class_name: str) -> set[str]:
        """Linkage classes that are class_name or inherit from it"""
        found = set()
        for linked in self.sprites_of_class:
            ancestor = linked
            while ancestor:
                if ancestor == class_name:
                    found.add(linked)
                    break
                ancestor = self.superclass.get(ancestor)
        return found

    def sprites_linked_to(self, class_name: str) -> set[int]:
        """Character ids whose linkage class is class_name or a subclass of it"""
        ids = set()
        for linked in self.subclasses_of(class_name):
            ids.update(self.sprites_of_class[linked])
        return ids

    def descendants_by_instance_name(self, class_name: str) -> dict[str, set[int]]:
        """Instance name -> {character ids}, for everything placed (at any depth) below the
        sprites linked to class_name or its subclasses"""
        if class_name in self._descendants_cache:
            return self._descendants_cache[class_name]
        seen, pending, by_name = set(), list(self.sprites_linked_to(class_name)), defaultdict(set)
        while pending:
            parent = pending.pop()
            if parent in seen:
                continue
            seen.add(parent)
            for child, name in self.placements.get(parent, ()):
                if name:
                    by_name[name].add(child)
                pending.append(child)
        self._descendants_cache[class_name] = by_name
        return by_name
