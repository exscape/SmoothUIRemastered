"""SWF tag lists: parsing, serializing, and decoding the simple tags the patchers need"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .build_common import fail

# Every tag starts with a 16-bit little-endian header: the tag code in the upper 10 bits
# and the payload length in the lower 6 bits. A length of 0x3F is a marker for the
# "long form", where the real length follows the header as a UI32.
TAG_LENGTH_BITS = 6
TAG_LENGTH_MASK = (1 << TAG_LENGTH_BITS) - 1
LONG_FORM_MARKER = TAG_LENGTH_MASK
SHORT_HEADER_SIZE = 2
LONG_HEADER_SIZE = 6

# DefineSprite payload: SpriteId (UI16) and FrameCount (UI16), followed by the sprite's own tag list
SPRITE_FIELDS_SIZE = 4

# The tags that the patchers actually need
class TagCode(IntEnum):
    END = 0
    SHOW_FRAME = 1
    REMOVE_OBJECT = 5
    PLACE_OBJECT2 = 26
    REMOVE_OBJECT2 = 28
    DEFINE_SPRITE = 39
    FRAME_LABEL = 43
    PLACE_OBJECT3 = 70
    SYMBOL_CLASS = 76
    DO_ABC = 82

@dataclass(frozen=True)
class Tag:
    """One tag: its code and payload"""
    code: int
    payload: bytes
    # The tag was stored with the 6-byte header. That form is allowed for any length, so this is
    # kept to make unchanged tags serialize to exactly the original bytes.
    long_header: bool = False

    def to_bytes(self) -> bytes:
        return encode_tag_header(self.code, len(self.payload), force_long=self.long_header) + self.payload

def encode_tag_header(tag_code: int, length: int, *, force_long: bool = False) -> bytes:
    if force_long or length >= LONG_FORM_MARKER:
        return struct.pack("<HI", (tag_code << TAG_LENGTH_BITS) | LONG_FORM_MARKER, length)
    return struct.pack("<H", (tag_code << TAG_LENGTH_BITS) | length)

SHOW_FRAME_TAG = encode_tag_header(TagCode.SHOW_FRAME, 0)
END_TAG = encode_tag_header(TagCode.END, 0)

def read_tag(body: bytes, offset: int, limit: int) -> tuple[Tag, int]:
    """Parse the tag at body[offset]. The whole tag must fit before `limit`.

    Returns the tag and the offset just past it."""
    if offset + SHORT_HEADER_SIZE > limit:
        fail(f"truncated tag header at offset {offset}")
    (header,) = struct.unpack_from("<H", body, offset)
    code = header >> TAG_LENGTH_BITS
    length = header & TAG_LENGTH_MASK
    header_size = SHORT_HEADER_SIZE

    if length == LONG_FORM_MARKER:
        header_size = LONG_HEADER_SIZE
        if offset + LONG_HEADER_SIZE > limit:
            fail(f"truncated long tag header at offset {offset}")
        (length,) = struct.unpack_from("<I", body, offset + SHORT_HEADER_SIZE)

    payload_start = offset + header_size
    end = payload_start + length
    if end > limit:
        fail(f"tag {code} at offset {offset} ({length} bytes) extends past the end of its container")

    return Tag(code, bytes(body[payload_start:end]), header_size == LONG_HEADER_SIZE), end

def iter_tags(body: bytes, start: int, end: int | None = None):
    """Yield the tags of the tag list stored in body[start:end], up to and including End.

    Fails unless the list is well-formed: it must be terminated by an End tag, and that
    End tag must be the last thing in the range."""
    end = len(body) if end is None else end
    offset = start
    while True:
        if offset >= end:
            fail(f"tag list at offset {start} has no End tag")
        tag_start = offset
        tag, offset = read_tag(body, offset, end)
        yield tag
        if tag.code == TagCode.END:
            if offset != end:
                # I'm not sure if this is still possible after changing the parser to ignore data after
                # the declared end, but let's keep it just in case
                fail(f"{end - offset} unexpected bytes after the End tag at offset {tag_start}")
            return

def parse_tags(body: bytes, start: int, end: int | None = None) -> list[Tag]:
    """All tags of the tag list in body[start:end], including the final End tag"""
    return list(iter_tags(body, start, end))

def serialize_tags(tags) -> bytes:
    return b"".join(tag.to_bytes() for tag in tags)

def read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    """Read the NUL-terminated string at data[offset]; returns it and the offset past the NUL"""
    end = data.index(0, offset)
    return data[offset:end].decode("utf8", "replace"), end + 1

# --- Payloads of individual tags ---

def parse_sprite_header(tag: Tag) -> tuple[int, int]:
    """DefineSprite -> (character id, declared frame count)"""
    return struct.unpack_from("<HH", tag.payload, 0)

def parse_sprite_tags(tag: Tag) -> list[Tag]:
    """DefineSprite -> the sprite's own tag list"""
    return parse_tags(tag.payload, SPRITE_FIELDS_SIZE)

def build_sprite(character_id: int, frame_count: int, tags, *, long_header: bool = False) -> Tag:
    payload = struct.pack("<HH", character_id, frame_count) + serialize_tags(tags)
    return Tag(TagCode.DEFINE_SPRITE, payload, long_header)

def parse_frame_label(tag: Tag) -> str:
    return read_cstring(tag.payload, 0)[0]

def parse_symbol_class(tag: Tag) -> list[tuple[int, str]]:
    """SymbolClass -> [(character id, linkage class name)]"""
    (count,) = struct.unpack_from("<H", tag.payload, 0)
    offset = 2
    symbols = []
    for _ in range(count):
        (character_id,) = struct.unpack_from("<H", tag.payload, offset)
        name, offset = read_cstring(tag.payload, offset + 2)
        symbols.append((character_id, name))
    return symbols

def parse_remove_object_depth(tag: Tag) -> int:
    """RemoveObject (CharacterId, Depth) or RemoveObject2 (Depth) -> the depth that is cleared"""
    offset = 2 if tag.code == TagCode.REMOVE_OBJECT else 0
    return struct.unpack_from("<H", tag.payload, offset)[0]

def split_do_abc(tag: Tag) -> tuple[bytes, bytes]:
    """DoABC -> (prefix, ABC data). The prefix is the flags (UI32) and the NUL-terminated name."""
    name_end = tag.payload.index(0, 4)
    return tag.payload[:name_end + 1], tag.payload[name_end + 1:]
