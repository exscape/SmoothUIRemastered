"""PlaceObject2/3 tags: decoding them, and encoding the "move" form used for interpolated frames"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import NamedTuple

from .bits import BitReader, BitWriter, signed_bits_needed
from .swf_tags import Tag, TagCode, read_cstring

# PlaceObject2/3 flag byte
FLAG_MOVE = 0x01
FLAG_HAS_CHARACTER = 0x02
FLAG_HAS_MATRIX = 0x04
FLAG_HAS_COLOR_TRANSFORM = 0x08
FLAG_HAS_RATIO = 0x10
FLAG_HAS_NAME = 0x20
FLAG_HAS_CLIP_DEPTH = 0x40

# PlaceObject3's second flag byte. (Scaleform GFX sets HasImage without a class name string.)
FLAG3_HAS_CLASS_NAME = 0x08
# ... and the display options it can set, with the size of each field (None: the filter list, whose
# size varies), in the order the fields appear in the tag
DISPLAY_OPTIONS = {
    0x01: None,     # filter list
    0x02: 1,        # blend mode
    0x04: 1,        # cacheAsBitmap
    0x20: 1,        # visible
    0x40: 4,        # opaque background colour
}

# Sizes of the filter records by filter id (gradient and convolution filters vary, see _filter_list_end)
FILTER_SIZES = {0: 23, 1: 9, 2: 15, 3: 27, 6: 80}

PLACE_OBJECT_CODES = (TagCode.PLACE_OBJECT2, TagCode.PLACE_OBJECT3)

FIXED_ONE = 1 << 16     # 1.0 as a MATRIX scale (16.16 fixed point)
CXFORM_ONE = 1 << 8     # 1.0 as a colour transform multiplier (8.8 fixed point)


class Matrix(NamedTuple):
    """MATRIX record: scale/rotate terms in 16.16 fixed point, translation in twips"""
    scale_x: int = FIXED_ONE
    scale_y: int = FIXED_ONE
    rotate_skew0: int = 0
    rotate_skew1: int = 0
    translate_x: int = 0
    translate_y: int = 0


class ColorTransform(NamedTuple):
    """CXFORMWITHALPHA record: RGBA multipliers (8.8 fixed point), then RGBA addends"""
    mul_r: int = CXFORM_ONE
    mul_g: int = CXFORM_ONE
    mul_b: int = CXFORM_ONE
    mul_a: int = CXFORM_ONE
    add_r: int = 0
    add_g: int = 0
    add_b: int = 0
    add_a: int = 0

    @property
    def multipliers(self):
        return self[:4]

    @property
    def addends(self):
        return self[4:]


IDENTITY_MATRIX = Matrix()
IDENTITY_COLOR_TRANSFORM = ColorTransform()


@dataclass(frozen=True)
class PlaceObject:
    depth: int
    is_move: bool                   # modifies the object already at `depth`
    character_id: int | None        # character placed (or swapped in, when moving)
    matrix: Matrix | None
    color_transform: ColorTransform | None
    ratio: int | None               # morph ratio
    name: str | None                # instance name
    # PlaceObject3 display options set by the tag: ((flag, raw field bytes), ...); see DISPLAY_OPTIONS
    display_options: tuple = ()


def parse_place_object(tag: Tag) -> PlaceObject:
    """Decode the fields of a PlaceObject2/3 tag that the patcher uses"""
    data = tag.payload
    flags = data[0]
    offset = 1
    flags3 = 0
    if tag.code == TagCode.PLACE_OBJECT3:
        flags3 = data[1]
        offset = 2

    (depth,) = struct.unpack_from("<H", data, offset)
    offset += 2

    if tag.code == TagCode.PLACE_OBJECT3 and flags3 & FLAG3_HAS_CLASS_NAME:
        offset = read_cstring(data, offset)[1]

    character_id = None
    if flags & FLAG_HAS_CHARACTER:
        (character_id,) = struct.unpack_from("<H", data, offset)
        offset += 2

    matrix = None
    if flags & FLAG_HAS_MATRIX:
        matrix, offset = _read_matrix(data, offset)

    color_transform = None
    if flags & FLAG_HAS_COLOR_TRANSFORM:
        color_transform, offset = _read_color_transform(data, offset)

    ratio = None
    if flags & FLAG_HAS_RATIO:
        (ratio,) = struct.unpack_from("<H", data, offset)
        offset += 2

    name = None
    if flags & FLAG_HAS_NAME:
        name, offset = read_cstring(data, offset)

    display_options = []
    if flags3:
        if flags & FLAG_HAS_CLIP_DEPTH:
            offset += 2
        for flag, size in DISPLAY_OPTIONS.items():
            if flags3 & flag:
                end = _filter_list_end(data, offset) if size is None else offset + size
                display_options.append((flag, data[offset:end]))
                offset = end

    return PlaceObject(depth, bool(flags & FLAG_MOVE), character_id, matrix, color_transform, ratio, name,
                       tuple(display_options))


def _filter_list_end(data: bytes, offset: int) -> int:
    count = data[offset]
    offset += 1
    for _ in range(count):
        filter_id = data[offset]
        offset += 1
        if filter_id in (4, 7):     # gradient glow / bevel: colours and their ratios, then 19 bytes
            offset += 1 + data[offset] * 5 + 19
        elif filter_id == 5:        # convolution: the matrix size, then 4-byte values and 5 more bytes
            offset += 2 + (2 + data[offset] * data[offset + 1]) * 4 + 5
        else:
            offset += FILTER_SIZES[filter_id]
    return offset


def build_move(depth: int, matrix: Matrix | None = None, color_transform: ColorTransform | None = None,
               ratio: int | None = None, display_options: tuple = ()) -> Tag:
    """PlaceObject2 that changes the given properties of the object at `depth`, or a PlaceObject3
    if display options (see PlaceObject) are given, which it sets as well"""
    flags = FLAG_MOVE
    if matrix:
        flags |= FLAG_HAS_MATRIX
    if color_transform:
        flags |= FLAG_HAS_COLOR_TRANSFORM
    if ratio is not None:
        flags |= FLAG_HAS_RATIO

    flags3 = sum(flag for flag, _ in display_options)
    payload = bytes([flags, flags3] if display_options else [flags]) + struct.pack("<H", depth)
    if matrix:
        payload += _encode_matrix(matrix)
    if color_transform:
        payload += _encode_color_transform(color_transform)
    if ratio is not None:
        payload += struct.pack("<H", ratio)
    payload += b"".join(field for _, field in sorted(display_options))
    return Tag(TagCode.PLACE_OBJECT3 if display_options else TagCode.PLACE_OBJECT2, payload)


_KEEP = object()


def rewrite_place_object(tag: Tag, *, color_transform=_KEEP, display_options=_KEEP) -> Tag:
    """The PlaceObject2/3 tag with its colour transform and/or display options (see PlaceObject)
    replaced; None / () removes them. Other fields stay as they are."""
    data = tag.payload
    flags = data[0]
    flags3 = data[1] if tag.code == TagCode.PLACE_OBJECT3 else 0
    offset = 2 if tag.code == TagCode.PLACE_OBJECT3 else 1
    offset += 2     # depth
    if flags3 & FLAG3_HAS_CLASS_NAME:
        offset = read_cstring(data, offset)[1]
    if flags & FLAG_HAS_CHARACTER:
        offset += 2
    if flags & FLAG_HAS_MATRIX:
        offset = _read_matrix(data, offset)[1]
    head_end = offset
    old_color_transform = None
    if flags & FLAG_HAS_COLOR_TRANSFORM:
        old_color_transform, offset = _read_color_transform(data, offset)
    middle_start = offset
    if flags & FLAG_HAS_RATIO:
        offset += 2
    if flags & FLAG_HAS_NAME:
        offset = read_cstring(data, offset)[1]
    if flags & FLAG_HAS_CLIP_DEPTH:
        offset += 2
    middle = data[middle_start:offset]
    old_options = []
    for flag, size in DISPLAY_OPTIONS.items():
        if flags3 & flag:
            end = _filter_list_end(data, offset) if size is None else offset + size
            old_options.append((flag, data[offset:end]))
            offset = end
    tail = data[offset:]    # clip actions (AS2 only)

    color_transform = old_color_transform if color_transform is _KEEP else color_transform
    display_options = tuple(old_options) if display_options is _KEEP else tuple(display_options)
    flags = flags & ~FLAG_HAS_COLOR_TRANSFORM | (FLAG_HAS_COLOR_TRANSFORM if color_transform else 0)
    flags3 = flags3 & ~sum(DISPLAY_OPTIONS) | sum(flag for flag, _ in display_options)
    code = TagCode.PLACE_OBJECT3 if tag.code == TagCode.PLACE_OBJECT3 or display_options else TagCode.PLACE_OBJECT2
    payload = (bytes([flags, flags3] if code == TagCode.PLACE_OBJECT3 else [flags])
               + data[2 if tag.code == TagCode.PLACE_OBJECT3 else 1:head_end]
               + (_encode_color_transform(color_transform) if color_transform else b"")
               + middle + b"".join(field for _, field in sorted(display_options)) + tail)
    return Tag(code, payload)


def _read_matrix(data: bytes, offset: int) -> tuple[Matrix, int]:
    bits = BitReader(data, offset)
    scale_x = scale_y = FIXED_ONE
    skew0 = skew1 = 0
    if bits.read(1):    # HasScale
        width = bits.read(5)
        scale_x = bits.read_signed(width)
        scale_y = bits.read_signed(width)
    if bits.read(1):    # HasRotate
        width = bits.read(5)
        skew0 = bits.read_signed(width)
        skew1 = bits.read_signed(width)
    width = bits.read(5)
    translate_x = bits.read_signed(width)
    translate_y = bits.read_signed(width)
    return Matrix(scale_x, scale_y, skew0, skew1, translate_x, translate_y), bits.align()


def _read_color_transform(data: bytes, offset: int) -> tuple[ColorTransform, int]:
    bits = BitReader(data, offset)
    has_add = bits.read(1)
    has_mul = bits.read(1)
    width = bits.read(4)
    mul = [bits.read_signed(width) for _ in range(4)] if has_mul else list(IDENTITY_COLOR_TRANSFORM.multipliers)
    add = [bits.read_signed(width) for _ in range(4)] if has_add else list(IDENTITY_COLOR_TRANSFORM.addends)
    return ColorTransform(*mul, *add), bits.align()


def _encode_matrix(matrix: Matrix) -> bytes:
    bits = BitWriter()
    if matrix.scale_x != FIXED_ONE or matrix.scale_y != FIXED_ONE:
        width = signed_bits_needed([matrix.scale_x, matrix.scale_y])
        bits.write(1, 1)
        bits.write(width, 5)
        bits.write_signed(matrix.scale_x, width)
        bits.write_signed(matrix.scale_y, width)
    else:
        bits.write(0, 1)

    if matrix.rotate_skew0 or matrix.rotate_skew1:
        width = signed_bits_needed([matrix.rotate_skew0, matrix.rotate_skew1])
        bits.write(1, 1)
        bits.write(width, 5)
        bits.write_signed(matrix.rotate_skew0, width)
        bits.write_signed(matrix.rotate_skew1, width)
    else:
        bits.write(0, 1)

    width = signed_bits_needed([matrix.translate_x, matrix.translate_y])
    bits.write(width, 5)
    bits.write_signed(matrix.translate_x, width)
    bits.write_signed(matrix.translate_y, width)
    return bits.to_bytes()


def _encode_color_transform(cxform: ColorTransform) -> bytes:
    has_mul = cxform.multipliers != IDENTITY_COLOR_TRANSFORM.multipliers
    has_add = cxform.addends != IDENTITY_COLOR_TRANSFORM.addends
    values = (list(cxform.multipliers) if has_mul else []) + (list(cxform.addends) if has_add else [])
    width = signed_bits_needed(values) if values else 1

    bits = BitWriter()
    bits.write(int(has_add), 1)
    bits.write(int(has_mul), 1)
    bits.write(width, 4)
    for value in values:
        bits.write_signed(value, width)
    return bits.to_bytes()
