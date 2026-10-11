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

# The tags that the rest of the script actually needs
class TagCode(IntEnum):
    END = 0
    SHOW_FRAME = 1
    PLACE_OBJECT2 = 26
    DEFINE_SPRITE = 39
    FRAME_LABEL = 43

@dataclass(frozen=True)
class Tag:
    """Location of one tag inside a body"""
    code: int
    start: int          # offset of the tag header
    header_size: int    # SHORT_HEADER_SIZE or LONG_HEADER_SIZE
    length: int         # payload length in bytes

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def end(self) -> int:
        return self.payload_start + self.length
def encode_tag_header(tag_code: int, length: int, *, force_long: bool = False) -> bytes:
    if force_long or length >= LONG_FORM_MARKER:
        return struct.pack("<HI", (tag_code << TAG_LENGTH_BITS) | LONG_FORM_MARKER, length)
    return struct.pack("<H", (tag_code << TAG_LENGTH_BITS) | length)

SHOW_FRAME_TAG = encode_tag_header(TagCode.SHOW_FRAME, 0)
END_TAG = encode_tag_header(TagCode.END, 0)

def read_tag(body: bytearray, offset: int, limit: int) -> Tag:
    """Parse the tag header at body[offset]. The whole tag must fit before `limit`."""
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

    tag = Tag(code, offset, header_size, length)
    if tag.end > limit:
        fail(f"tag {code} at offset {offset} ({length} bytes) extends past the end of its container")
    return tag

def iter_tags(body: bytearray, start: int, end: int):
    """Yield the tags of the tag list stored in body[start:end], up to and including End.

    Fails unless the list is well-formed: it must be terminated by an End tag, and that
    End tag must be the last thing in the range."""
    offset = start
    while True:
        if offset >= end:
            fail(f"tag list at offset {start} has no End tag")
        tag = read_tag(body, offset, end)
        yield tag
        offset = tag.end
        if tag.code == TagCode.END:
            if offset != end:
                # I'm not sure if this is still possible after changing the parser to ignore data after
                # the declared end, but let's keep it just in case
                fail(f"{end - offset} unexpected bytes after the End tag at offset {tag.start}")
            return
