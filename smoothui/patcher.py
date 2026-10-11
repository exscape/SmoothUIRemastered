"""Patch UI frame rates in .redswf files, keeping each file's size identical"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

from zopfli.zlib import compress as zopfli_compress

# Note: terminology may be a bit off in this file; "movie" may be preferred in some of all places where "SWF" is used,
# but it really doesn't sound right to me.
#
# Every SWF/GFX file starts with: 3-byte signature, 1-byte version, 4-byte total length
HEADER_SIZE = 8
MIN_VERSION, MAX_VERSION = 1, 40
SIGS = (b"GFX", b"FWS", b"CFX", b"CWS")
COMPRESSED_SIGS = (b"CFX", b"CWS")

# Sanity floor for the declared length of an uncompressed SWF: the header, a minimal
# frame-size rectangle, the frame rate and frame count, and at least a few tags
MIN_UNCOMPRESSED_LENGTH = 21

# The frame rate is stored as an unsigned 8.8 fixed-point number
FPS_FIXED_POINT_SCALE = 256
MAX_RAW_FPS = 0xFFFF

# Dev hooks for manually exploring and/or editing a target SWF payload
# DEV_MODE:
#   None  : off; read the payload, patch it, write it back
#   dump  : write the target decompressed SWF to DEV_OUTPUT_FILE and exit
#   insert: read the SWF back from DEV_OUTPUT_FILE and insert it into the .redswf
#   pause : dump, wait for the user to type "continue", then read it back and keep going
DEV_MODE = None
DEV_TARGET_RAW = None   # raw name given to set_dev (e.g. "panel_inventory")
DEV_TARGET_FILE = None  # resolved full relative path (e.g. "gameplay/gui_new/swf/inventory/panel_inventory.redswf")
DEV_OUTPUT_FILE = None  # output filename, no relative path (e.g. "panel_inventory_payload.swf")

def set_dev(mode, target_file_name):
    """Called by the build scripts to enable DEV_MODE"""
    global DEV_MODE, DEV_TARGET_RAW
    DEV_MODE = mode
    DEV_TARGET_RAW = target_file_name

def _dev_matches(rel_key, target):
    """Check whether a normalized profile key matches a normalized dev target name.

    rel_key: normalized profile path, e.g. "gameplay/gui_new/swf/inventory/panel_inventory.redswf"
    target: user input, e.g. "panel_inventory", "inventory/panel_inventory.redswf"
    """
    # Exact path-suffix match (target already includes extension or is a full sub-path)
    if rel_key.endswith("/" + target):
        return True

    # Target is a base name without extension → match .redswf files
    return rel_key.endswith("/" + target + ".redswf")

def _resolve_dev_target(profile):
    """Resolve DEV_TARGET_RAW against profile keys. Sets DEV_TARGET_FILE and DEV_OUTPUT_FILE.

    Fails if the the user input matches anything except exactly one target .redswf.
    """
    global DEV_TARGET_FILE, DEV_OUTPUT_FILE
    target_norm = DEV_TARGET_RAW.replace("\\", "/").lower().strip("/")
    matches = [rel for rel in profile if _dev_matches(profile_key(rel), target_norm)]
    if len(matches) == 0:
        raise ValueError(f"Dev mode: '{DEV_TARGET_RAW}' matched no files in profile")
    if len(matches) > 1:
            raise ValueError(f"Dev mode: '{DEV_TARGET_RAW}' is ambiguous: it matches "
                             f"{len(matches)} files in profile; need exactly one.")
    DEV_TARGET_FILE = matches[0]
    DEV_OUTPUT_FILE = Path(DEV_TARGET_FILE).name.replace(".redswf", "_payload.swf")

def build_dev_file_bytes(header, body):
    """Build a valid, *uncompressed* SWF/GFX file from the container's header and the
    decompressed body, so that it can be edited (e.g. with FFDec, or a hex editor).

    The container header will be CWS/CFX (compressed), but the body here is already
    decompressed, so the signature must be switched to FWS/GFX. The declared length is
    the total uncompressed size, which is the same for both forms."""
    header = bytes(header)
    UNCOMPRESSED_SIG_MAP = {b"CWS": b"FWS", b"CFX": b"GFX"}
    sig = UNCOMPRESSED_SIG_MAP[header[:3]]
    version = header[3:4]
    return sig + version + struct.pack("<I", HEADER_SIZE + len(body)) + bytes(body)

def dev_read_body(saved):
    """Extract the SWF body from a file that was dumped and then edited/saved by a tool.

    Accepts uncompressed (FWS/GFX) as well as zlib-compressed (CWS/CFX) files, since
    external tools may save in either form. The header is ignored beyond its signature."""
    if len(saved) < HEADER_SIZE:
        raise ValueError("Dev mode: edited file is too short to contain an SWF header")
    sig = saved[:3]
    if sig in (b"FWS", b"GFX"):
        return bytearray(saved[HEADER_SIZE:])
    if sig in COMPRESSED_SIGS:
        d = zlib.decompressobj()
        try:
            body = d.decompress(saved[HEADER_SIZE:])
        except zlib.error as e:
            raise ValueError(f"Dev mode: edited file has a compressed signature but bad zlib data: {e}")
        if not d.eof:
            raise ValueError("Dev mode: edited file's zlib stream is truncated")
        return bytearray(body)
    raise ValueError(f"Dev mode: unsupported signature {sig!r} in edited file "
                     "(LZMA 'ZWS' is not supported; save as uncompressed or zlib)")

def dev_hook(header, body):
    p = Path(DEV_OUTPUT_FILE)

    if DEV_MODE == "dump":
        p.write_bytes(build_dev_file_bytes(header, body))
        print(f"Dev mode: wrote {p} ({HEADER_SIZE} header bytes + {len(body)} body bytes). "
              f"Edit it (e.g. in FFDec), then rerun with --dev-insert {p.name} to keep patching.")
        sys.exit(0)
    if DEV_MODE == "insert":
        saved = p.read_bytes()
        new_body = dev_read_body(saved)
        print(f"Dev mode: using {p} ({len(saved)} bytes); body = {len(new_body)} bytes.")
        return new_body
    if DEV_MODE == "pause":
        p.write_bytes(build_dev_file_bytes(header, body))
        print(f"Dev mode: wrote {p} ({HEADER_SIZE} header bytes + {len(body)} body bytes). "
              "Edit it, then type 'continue' on a line of its own to resume "
              "(anything else is ignored).")

        while True:
            try:
                line = sys.stdin.readline()
            except KeyboardInterrupt:
                print(f"\n\nAborting; file {p.name} remains on disk.")
                sys.exit(0)
            if not line:
                continue # Can this happen?
            if line.strip().lower() == "continue":
                break
            else:
                print("Not recognized. Type \"continue\", or abort with ^C.")
        return dev_read_body(p.read_bytes())
    return body

def profile_key(rel):
    """One comparable spelling for a profile path: lowercase, slash-separated"""
    return str(rel).replace("\\", "/").lower().strip("/")

def file_target_fps(profile, rel):
    """The frame rate the profile states for this file"""
    target = profile.get(profile_key(rel))
    assert target is not None
    return float(target)

def patch_files(src, dst, profile, *, dry_run=False, on_file=None):
    """Patch the given files (paths relative to src), writing changed ones to dst with the
    same relative path. profile maps each file to its target frame rate; files that are not in
    it, or that are already at/above that rate, are not written.

    on_file(rel, status, old_fps, new_fps, note) is called as soon as each file's result is
    known; status is "patched", "kept" or "failed".

    Returns (patched, kept, failed): lists of relative paths, except that failed holds
    (rel, message) tuples. Raises NotADirectoryError / ValueError for invalid directories.
    """
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if not src.is_dir():
        raise NotADirectoryError(f"Not a directory: {src}")
    if dst == src or src in dst.parents:
        raise ValueError("Output dir must not be inside the input dir.")

    # Resolve dev target against the profile
    if DEV_MODE is not None and DEV_TARGET_RAW is not None:
        _resolve_dev_target(profile)

    patched, kept, failed = [], [], []

    def report(rel, status, old=None, new=None, note=""):
        if on_file:
            on_file(rel, status, old, new, note)

    for rel in map(Path, profile.keys()):
        orig = (src / rel).read_bytes()
        data = bytearray(orig)
        try:
            target_fps = file_target_fps(profile, rel)
            assert target_fps >= 1 and target_fps <= 256
            result = process(data, target_fps)
        except ValueError as e:
            failed.append((rel, str(e)))
            report(rel, "failed", note=str(e))
            continue

        old_fps, new_fps, note = result
        if new_fps is None:
            kept.append(rel)
            report(rel, "kept", old_fps, note=note)
            continue

        if not dry_run:
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        patched.append({"path": rel, "fps": new_fps})
        report(rel, "patched", old_fps, new_fps, note)

    return patched, kept, failed

def process(data, new_fps, rel):
    """Find the SWF payload embedded in data and set its frame rate. Patches data in place.

    Returns (old_fps, new_fps, note). Raises ValueError if there isn't exactly one valid SWF.
    """
    result = None
    for sig in SIGS:
        pos = data.find(sig)
        while pos != -1:
            # This does patch the second payload, if any, but so far there has never *been* two.
            # The check is more for safety: since we're binary patching inside the container, it's
            # conceivable that we will find a seemingly valid SWF in random data and "patch" it,
            # and break the mod. It's much better to fail in this unlikely case.
            payload = read_and_patch_swf(data, pos, sig, new_fps, rel)
            if payload is None:
                pos = data.find(sig, pos + len(sig))
                continue

            end, found = payload
            if found is not None:
                if result is not None:
                    raise ValueError("found more than 1 payload (expected 1)")
                result = found
            pos = data.find(sig, end)

    if result is None:
        raise ValueError("found 0 payloads (expected 1)")
    return result

def read_and_patch_swf(data, pos, sig, new_fps, rel):
    """Check for an SWF header at data[pos] and patch the payload if found."""
    if pos + HEADER_SIZE > len(data):
        return None
    if not MIN_VERSION <= data[pos + 3] <= MAX_VERSION:
        return None

    if sig not in COMPRESSED_SIGS:
        raise ValueError("SWF was not compressed: please open a GitHub issue that uncompressed SWFs needs to be supported!")

    return patch_compressed_swf(data, pos, new_fps, rel)

def patch_compressed_swf(data, pos, new_fps, rel):
    """Patch the zlib-compressed SWF whose header starts at data[pos].

    The patched stream is written back into exactly the space the original occupied,
    zero padded, so the file size never changes."""
    stream_start = pos + HEADER_SIZE
    decompressor = zlib.decompressobj()
    try:
        body = bytearray(decompressor.decompress(bytes(data[stream_start:])))
    except zlib.error:
        return None
    if not decompressor.eof:
        return None  # Truncated stream

    # Whatever the decompressor didn't consume is not part of this stream (typically
    # the zero padding from a previous patch, or the next embedded SWF)
    stream_len = len(data) - stream_start - len(decompressor.unused_data)
    stream_end = stream_start + stream_len

    patched = patch_body(body, new_fps)
    if patched is None:
        return stream_end, None
    old_fps, applied_fps = patched
    if old_fps == applied_fps:
        return stream_end, (old_fps, None, "fps already matches")

    # Dev mode: only apply the dev_hook to the specifically targeted file
    if DEV_MODE is not None and DEV_TARGET_FILE is not None and profile_key(rel) == profile_key(DEV_TARGET_FILE):
        body = dev_hook(data[pos:stream_start], body)

    compressed = recompress(body, stream_len)
    padding = stream_len - len(compressed)
    data[stream_start:stream_end] = compressed + b"\0" * padding
    return stream_end, (old_fps, applied_fps, f"{padding} pad bytes")

def patch_body(body, new_fps):
    """The method that actually patches the framerate bytes.

    Set the frame rate in an SWF body (the data following the 8-byte file header).
    Patches body in place.
    Returns (old_fps, new_fps) if body is a valid SWF stream (new_fps == old_fps when
    no change is needed), or None if body doesn't look like a valid SWF stream.
    """
    # The body starts with the frame-size RECT: a 5-bit field giving the bit width of each
    # of its four coordinates, then the coordinates themselves, padded to a whole byte.
    if not body:
        return None
    coord_bits = body[0] >> 3
    if coord_bits == 0:
        return None
    rect_bytes = (5 + 4 * coord_bits + 7) // 8

    # Directly after the RECT: frame rate (uint16, 8.8 fixed point), frame count (uint16)
    fps_offset = rect_bytes
    if fps_offset + 4 > len(body):
        return None
    raw_fps, frame_count = struct.unpack_from("<HH", body, fps_offset)
    if raw_fps == 0 or frame_count == 0:
        return None  # Not plausible for a real SWF; probably a false signature match

    old_fps = raw_fps / FPS_FIXED_POINT_SCALE
    if old_fps == new_fps:
        # No change needed; don't return None as that suggests something was wrong
        return old_fps, new_fps

    new_raw_fps = round(new_fps * FPS_FIXED_POINT_SCALE)
    if not 1 <= new_raw_fps <= MAX_RAW_FPS:
        raise ValueError(f"fps {new_fps:.2f} out of range (max 255.99)")
    struct.pack_into("<H", body, fps_offset, new_raw_fps)

    return raw_fps / FPS_FIXED_POINT_SCALE, new_raw_fps / FPS_FIXED_POINT_SCALE

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
        c = zlib.compressobj(9, zlib.DEFLATED, 15, mem)
        out = c.compress(raw) + c.flush()
        smallest = len(out) if smallest is None else min(smallest, len(out))
        if len(out) <= max_len:
            return out

    # zlib failed, output too large: try zopfli with increasing effort
    for iters in (1, 10, 40):
        out = zopfli_compress(raw, numiterations=iters)
        smallest = min(smallest, len(out))
        if len(out) <= max_len:
            return out

    raise ValueError(f"recompressed stream doesn't fit "
                     f"(smallest {smallest} bytes vs {max_len} allowed)")