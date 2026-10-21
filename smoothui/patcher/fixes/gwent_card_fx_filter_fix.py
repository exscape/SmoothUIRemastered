"""gwint_game: the Hero and Decoy card effects draw coloured boxes.

Scaleform draws a filtered object into a texture of its own, and there it mishandles the colour of
fully transparent pixels, which Flash ignores:

- CardFXHero: a band (sprite 307 in 5.01: a mask, the card shape turned on its side, sweeping up)
  reveals the card's orange edge glow, a bitmap, under a Blur filter. The glow's transparent texels
  hold leftover colours (mostly red), and Scaleform shows them, as a red box as wide as the bitmap
  that follows the band up the card. Where the same bitmap is drawn without a filter, it looks right.
  The fix removes the Blur, so the band has hard top and bottom edges, which only show at the sides
  of the card (the band runs under the card's grey overlay).
- CardFXDummy (Decoy): two ColorMatrix filters only brighten (+101 and +47 on R, G and B; alpha
  unchanged), and Scaleform brightens the transparent area of the filter texture too: a grey box
  over the effect. The fix replaces each with the colour transform that does the same to a grey
  (each row's sum as the multiplier, its offset as the addend). The +101 matrix also raises the
  saturation a little; its content is grey and white smoke, so that changes nothing visible.
"""

from __future__ import annotations

import struct

from ..swf_patcher.place_object import (
    CXFORM_ONE,
    FILTER_SIZES,
    PLACE_OBJECT_CODES,
    ColorTransform,
    parse_place_object,
    rewrite_place_object,
)
from ..swf_patcher.swf_tags import Tag
from .swf_edit import CantApply, FixKind, FixResult, SwfEdit

NAME = "gwent-card-fx-filter-fix"
CONSEQUENCE = "in Gwent, the Hero and Decoy card effects have graphical issues"
KIND = FixKind.GAME_BUG
HERO_CLASS = "CardFXHero"
DUMMY_CLASS = "CardFXDummy"
FILTER_LIST = 0x01      # the display option that holds the filter list
BLUR, COLOR_MATRIX = 1, 6


def apply(swf: SwfEdit) -> FixResult:
    sprites, details = [], []
    for class_name, fix_tags in ((HERO_CLASS, _drop_blur), (DUMMY_CLASS, _color_matrix_to_cxform)):
        sprite_id, tags = swf.linked_sprite(class_name)
        new_tags, detail = fix_tags(class_name, tags)
        if new_tags is not None:
            swf.replace_sprite(sprite_id, new_tags)
            sprites.append(sprite_id)
            details.append(f"{class_name}: {detail}")
    if not sprites:
        return FixResult([], f"{HERO_CLASS} has no Blur and {DUMMY_CLASS} no ColorMatrix filter")
    return FixResult(sprites, "; ".join(details))


def _filters(tag: Tag) -> list[tuple[int, bytes]]:
    """[(filter id, its record after the id)] of a PlaceObject's filter list (none: [])"""
    raw = dict(parse_place_object(tag).display_options).get(FILTER_LIST)
    if raw is None:
        return []
    filters, offset = [], 1
    for _ in range(raw[0]):
        filter_id = raw[offset]
        if filter_id not in FILTER_SIZES:
            raise CantApply(f"unexpected filter {filter_id}")
        filters.append((filter_id, raw[offset + 1:offset + 1 + FILTER_SIZES[filter_id]]))
        offset += 1 + FILTER_SIZES[filter_id]
    return filters


def _without_filters(tag: Tag, **changes) -> Tag:
    options = tuple((flag, field) for flag, field in parse_place_object(tag).display_options if flag != FILTER_LIST)
    return rewrite_place_object(tag, display_options=options, **changes)


def _filtered_depths(tags: list[Tag], filter_id: int) -> set[int]:
    return {parse_place_object(tag).depth for tag in tags
            if tag.code in PLACE_OBJECT_CODES and any(fid == filter_id for fid, _ in _filters(tag))}


def _drop_blur(class_name: str, tags: list[Tag]):
    depths = _filtered_depths(tags, BLUR)
    if not depths:
        return None, "no Blur"
    if len(depths) != 1:
        raise CantApply(f"{class_name} has Blur filters at depths {sorted(depths)}, expected one")
    depth = depths.pop()
    new_tags = []
    for tag in tags:
        if tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).depth == depth and _filters(tag):
            if [fid for fid, _ in _filters(tag)] != [BLUR]:
                raise CantApply(f"{class_name}'s depth {depth} has other filters than a Blur")
            tag = _without_filters(tag)
        new_tags.append(tag)
    return new_tags, f"removed the Blur filter at depth {depth}"


def _brightness_cxform(class_name: str, depth: int, record: bytes) -> ColorTransform:
    """The colour transform that does what a ColorMatrix filter does to a grey, if it leaves alpha alone"""
    m = struct.unpack("<20f", record)
    rows = [m[i * 5:i * 5 + 5] for i in range(4)]
    if rows[3] != (0, 0, 0, 1, 0) or any(row[3] for row in rows[:3]):
        raise CantApply(f"{class_name}'s ColorMatrix at depth {depth} changes alpha")
    mul = [round(sum(row[:3]) * CXFORM_ONE) for row in rows[:3]]
    add = [round(row[4]) for row in rows[:3]]
    return ColorTransform(*mul, CXFORM_ONE, *add, 0)


def _color_matrix_to_cxform(class_name: str, tags: list[Tag]):
    depths = _filtered_depths(tags, COLOR_MATRIX)
    if not depths:
        return None, "no ColorMatrix"
    new_tags, done = [], []
    for tag in tags:
        if tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).depth in depths:
            place = parse_place_object(tag)
            filters = _filters(tag)
            if place.color_transform is not None:
                # The colour transform would replace the one standing in for the filter
                raise CantApply(f"{class_name}'s depth {place.depth} has a colour transform")
            if filters:
                if [fid for fid, _ in filters] != [COLOR_MATRIX]:
                    raise CantApply(f"{class_name}'s depth {place.depth} has other filters than a ColorMatrix")
                cxform = _brightness_cxform(class_name, place.depth, filters[0][1])
                tag = _without_filters(tag, color_transform=cxform)
                done.append((place.depth, cxform))
        new_tags.append(tag)
    return new_tags, ", ".join(f"depth {depth}: ColorMatrix -> colour transform x{cxform.mul_r / CXFORM_ONE:.3f} "
                               f"+{cxform.add_r}" for depth, cxform in done)
