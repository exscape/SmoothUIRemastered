"""Patch UI frame rates in .redswf files, keeping each file's size identical"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

from smoothui.swf import (
    COMPRESSED_SIGS,
    HEADER_SIZE,
    MAX_VERSION,
    MIN_VERSION,
    SIGS,
    swf_get_header_offsets,
    swf_patch,
)

# Note: terminology may be a bit off in this file; "movie" may be preferred in some of all places where "SWF" is used,
# but it really doesn't sound right to me.
#
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

def dev_make_body_hook(rel):
    """Return a dev_hook wrapper for this file, or None if it isn't the dev target."""
    if DEV_MODE is None or DEV_TARGET_FILE is None:
        return None
    if profile_key(rel) != profile_key(DEV_TARGET_FILE):
        return None
    return dev_hook

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
            print(f"********** START PROCESSING {rel} ************")
            result = process(data, target_fps, dev_make_body_hook(rel))
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
        print(f"********** DONE PROCESSING {rel} ************")

    return patched, kept, failed

def process(data, new_fps, rel):
    """Find the SWF payload embedded in data and set its frame rate. Patches data in place.

    Returns (old_fps, new_fps, note). Raises ValueError if there isn't exactly one valid SWF.
    """
    result = None
    for sig in SIGS:
        pos = data.find(sig)
        while pos != -1:
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

    return patch_fps(data, pos, new_fps, rel)

def patch_fps(data, pos, new_fps, body_hook=None):
    """Set the frame rate of the compressed SWF at data[pos].

    Returns:
    None if there is no valid SWF stream at pos
    (stream_end, found) where found is None if the body doesn't look like an SWF, or
    (old_fps, new_fps_or_None, note).
    new_fps is None only when nothing at all changed."""

    def patch(body):
        patched = patch_fps_in_body(body, new_fps)
        if patched is None:
            return None
        old_fps, applied_fps = patched

        if old_fps == applied_fps:
            return True, (old_fps, applied_fps, "fps already matches")
        return True, (old_fps, applied_fps, None)

    out = swf_patch(data, pos, patch, body_hook)
    if out is None:
        return None
    stream_end, result, padding = out
    if result is None:
        return stream_end, None
    old_fps, applied_fps, note = result
    if padding is not None:
        note = f"{note}, {padding} pad bytes" if note else f"{padding} pad bytes"
    return stream_end, (old_fps, applied_fps, note)

def patch_fps_in_body(body, new_fps):
    """The method that actually patches the framerate bytes.

    Set the frame rate in an SWF body (the data following the 8-byte file header).
    Patches body in place.
    Returns (old_fps, new_fps) if body is a valid SWF stream (new_fps == old_fps when
    no change is needed), or None if body doesn't look like a valid SWF stream.
    """
    fps_offset, _ = swf_get_header_offsets(body)
    if fps_offset is None:
        return None

    raw_fps, frame_count = struct.unpack_from("<HH", body, fps_offset)
    if raw_fps == 0 or frame_count == 0:
        # Not plausible for a real SWF; probably a false signature match
        return None

    old_fps = raw_fps / FPS_FIXED_POINT_SCALE
    if old_fps == new_fps:
        # No change needed; don't return None as that suggests something was wrong
        return old_fps, new_fps

    new_raw_fps = round(new_fps * FPS_FIXED_POINT_SCALE)
    if not 1 <= new_raw_fps <= MAX_RAW_FPS:
        raise ValueError(f"fps {new_fps:.2f} out of range (max 255.99)")
    struct.pack_into("<H", body, fps_offset, new_raw_fps)

    return raw_fps / FPS_FIXED_POINT_SCALE, new_raw_fps / FPS_FIXED_POINT_SCALE