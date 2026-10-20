"""Low-level SWF manipulation code, used by the patchers"""

import struct
import zlib

from ..common import SmoothUIError


class MalformedSwfError(SmoothUIError):
    """The SWF data can't be read: it's broken, or in a form the patcher doesn't support"""

# Every SWF/GFX file starts with: 3-byte signature, 1-byte version, 4-byte total length
SWF_HEADER_SIZE = 8
valid_versions = range(1, 41)
SWF_SIGS = (b"GFX", b"FWS", b"CFX", b"CWS")
SWF_COMPRESSED_SIGS = (b"CFX", b"CWS")
# The signature of the same SWF when stored uncompressed
SWF_UNCOMPRESSED_SIGS = {b"CWS": b"FWS", b"CFX": b"GFX"}

# The frame rate is stored as an unsigned 8.8 fixed-point number
FPS_FIXED_POINT_SCALE = 256
MAX_RAW_FPS = 0xFFFF

# Sanity floor for the declared length of an uncompressed SWF: the header, a minimal
# frame-size rectangle, the frame rate and frame count, and at least a few tags
MIN_UNCOMPRESSED_LENGTH = 21

def swf_patch(data, pos, patch, body_hook=None):
    """Apply an arbitrary patch to the zlib-compressed SWF whose header starts at data[pos].
    data itself is not modified.

    patch(body) receives the decompressed body (a bytearray, the data after the 8-byte
    header) and may modify it in place, including changing its length. It returns:
      - None                  if body isn't something it can/should patch (nothing is written)
      - (False, result)       if it looked at body but left it unchanged (nothing is written)
      - (True, result)        if body was modified (it will be recompressed and written back)
    `result` is opaque to this function and is passed through to the caller.

    body_hook(header, body), if given, is called after a successful patch, just before
    recompression. header is the original 8-byte container header, body is the patched
    body. It returns the body to use (possibly a different bytearray, of a different
    length), so it can be used for debugging or manual editing.

    Returns None if there is no complete zlib stream at pos (not an SWF payload).
    Otherwise returns (stream_end, result, new_swf_bytes): new_swf_bytes is the new header +
    compressed stream, or None if body was not modified. result is None if patch returned None.
    """
    stream_start = pos + SWF_HEADER_SIZE
    decompressor = zlib.decompressobj()
    try:
        body = bytearray(decompressor.decompress(bytes(data[stream_start:])))
    except zlib.error:
        return None
    if not decompressor.eof:
        return None  # Truncated stream

    stream_len = len(data) - stream_start - len(decompressor.unused_data)
    stream_end = stream_start + stream_len

    # Every single file .redswf (as of game version 5.01) contains stale data! Let's drop it.
    # For example, panel_alchemy.redswf has 2069 bytes of junk data following the end of the SWF
    # payload. Nothing in the game would read them, so I see no reason to preserve them.
    # The 20 files patched (as of v0.3 development) range between 24 and 3927 bytes of trailing junk.
    (declared_total,) = struct.unpack_from("<I", data, pos + 4)
    declared_body_len = declared_total - SWF_HEADER_SIZE
    if declared_body_len < 0 or declared_body_len > len(body):
        raise MalformedSwfError(f"declared length {declared_total} doesn't match the decompressed size "
                                f"{SWF_HEADER_SIZE + len(body)}")
    del body[declared_body_len:]

    outcome = patch(body)
    if outcome is None:
        return stream_end, None, None
    modified, result = outcome
    if not modified:
        return stream_end, result, None

    # Runs before the length fix-up below, because the hook may change the body's length
    if body_hook is not None:
        body = body_hook(data[pos:stream_start], body)

    # The header's length field is the total uncompressed length; the body's length may have changed
    header = bytearray(data[pos:stream_start])
    header[4:8] = (SWF_HEADER_SIZE + len(body)).to_bytes(4, "little")

    compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 8)
    compressed_body = compressor.compress(bytes(body)) + compressor.flush()
    new_swf_bytes = header + compressed_body

    return stream_end, result, new_swf_bytes

def swf_get_header_offsets(body: bytearray):
    """
    Parses the uncompressed SWF MovieHeader to find the byte offsets for
    the frame rate (FPS) and the start of the tag stream.

    Returns (fps_offset, first_tag_offset) or (None, None) if the stream is invalid.
    """
    if not body:
        return None, None

    # The body starts with the frame-size RECT: a 5-bit field giving the bit width
    # of each of its four coordinates, then the coordinates themselves, padded to a whole byte.
    coord_bits = body[0] >> 3
    if coord_bits == 0:
        return None, None

    rect_bytes = (5 + 4 * coord_bits + 7) // 8

    # Directly after the RECT: frame rate (uint16, 8.8 fixed point), frame count (uint16)
    fps_offset = rect_bytes
    first_tag_offset = fps_offset + 4

    if first_tag_offset > len(body):
        return None, None

    # This could return first_tag_offset, since fps_offset = first_tag_offset = 4.
    # Not sure which I prefer.
    return fps_offset, first_tag_offset