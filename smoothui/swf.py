"""Low-level SWF manipulation code, used by the patchers"""

import struct
import zlib

from zopfli.zlib import compress as zopfli_compress

from .build_common import fail

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
    """Apply an arbitrary patch to the zlib-compressed SWF whose header starts
    at data[pos], writing the result back into exactly the space the original
    occupied (zero padded), so the file size never changes.

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
    Otherwise returns (stream_end, result, padding, new_swf_bytes): padding is the number of
    zero pad bytes written, and new_swf_bytes the new header + compressed stream; both are None
    if nothing was written. result is None if patch returned None.
    Raises ValueError if the patched body no longer fits.
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
        fail(f"declared length {declared_total} doesn't match the decompressed size {SWF_HEADER_SIZE + len(body)}")
    if declared_body_len < len(body):
        print(f"[!] Dropping {len(body) - declared_body_len} bytes after the declared end of the SWF")
        del body[declared_body_len:]

    old_body_len = len(body)
    outcome = patch(body)
    if outcome is None:
        return stream_end, None, None, None
    modified, result = outcome
    if not modified:
        return stream_end, result, None, None

    # Runs before the length fix-up below, because the hook may change the body's length
    if body_hook is not None:
        body = body_hook(data[pos:stream_start], body)

    if len(body) != old_body_len:
        print(f"*** Updated body length: {old_body_len} => {len(body)} bytes ({old_body_len - len(body):+} byte delta from patch")
        data[pos + 4:pos + 8] = (SWF_HEADER_SIZE + len(body)).to_bytes(4, "little")

    compressed = recompress(body, stream_len)
    padding = stream_len - len(compressed)

    data[stream_start:stream_end] = compressed + b"\0" * padding # TODO: not used with new patcher; change patcher OR make data read only
    new_swf_bytes = data[pos:pos+SWF_HEADER_SIZE] + compressed

    return stream_end, result, padding, new_swf_bytes

def recompress(body, max_len):
    """Recompress the patched data to fit in the same size as the original.

    We can't resize the container, so we *need* the data to fit in the original bytes.
    However, since we've just changed one or two bytes, that's almost certainly possible,
    especially using zopfli which compresses better than the zlib typically used by default.
    """
    raw = bytes(body)
    smallest = None

    # try zlib first
    # mem 8 often compresses slightly better(?!) and often yields the exact size
    # of the original blob, so let's start there
    for mem in (8, 9):
        print(".")
        c = zlib.compressobj(9, zlib.DEFLATED, 15, mem)
        out = c.compress(raw) + c.flush()
        return out




        smallest = len(out) if smallest is None else min(smallest, len(out))
        if len(out) <= max_len:
            return out

    # zlib failed, output too large: try zopfli with increasing effort
    for iters in (1,):# 10, 40):
        print("*")
        out = zopfli_compress(raw, numiterations=iters)
        return out
        smallest = min(smallest, len(out))
        if len(out) <= max_len:
            return out

    fail(f"recompressed stream doesn't fit "
                     f"(smallest {smallest} bytes vs {max_len} allowed)")

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