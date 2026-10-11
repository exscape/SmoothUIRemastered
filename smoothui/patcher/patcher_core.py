"""Patch the UI in .redswf files, keeping each file's size identical"""

from __future__ import annotations

import struct
from pathlib import Path

from ..common import profile_key
from ..swf import (
    SWF_COMPRESSED_SIGS,
    SWF_HEADER_SIZE,
    SWF_SIGS,
    swf_get_header_offsets,
    swf_patch,
    valid_versions,
)
from .dev_mode import dev_make_body_hook, resolve_dev_target
from .patcher_freeze import freeze_focus_animations

# The frame rate is stored as an unsigned 8.8 fixed-point number
FPS_FIXED_POINT_SCALE = 256
MAX_RAW_FPS = 0xFFFF

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

    resolve_dev_target(profile)

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
    for sig in SWF_SIGS:
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
    if pos + SWF_HEADER_SIZE > len(data):
        return None
    if not data[pos + 3] in valid_versions:
        return None

    if sig not in SWF_COMPRESSED_SIGS:
        raise ValueError("SWF was not compressed: please open a GitHub issue that uncompressed SWFs needs to be supported!")

    return patch_fps(data, pos, new_fps, rel)

def patch_fps(data, pos, new_fps, body_hook=None):
    """Set the frame rate of the compressed SWF at data[pos] and freeze focus animations.

    The freeze always runs, even when the fps already matches. This likely won't matter, but
    it gives UX consistency for files where the fps isn't changed.

    Returns:
    None if there is no valid SWF stream at pos
    (stream_end, found) where found is None if the body doesn't look like an SWF, or
    (old_fps, new_fps_or_None, note).
    new_fps is None only when nothing at all changed;
    if just the freeze changed something, new_fps equals old_fps, so the file still
    counts as patched and gets written."""

    def patch(body):
        before = bytes(body)

        patched = patch_fps_in_body(body, new_fps)
        if patched is None:
            return None
        old_fps, applied_fps = patched

        # Freeze focus animations etc.
        # Do this for all files, for UX consistency. (Won't actually matter unless we release a lower fps version, though.)
        old_len = len(body)
        freeze_focus_animations(body)
        if len(body) > old_len:
            raise AssertionError("Body got BIGGER from patch")

        if body == before:
            return False, (old_fps, None, "nothing to patch")

        if old_fps == applied_fps:
            return True, (old_fps, applied_fps, "fps already matches; freeze applied")
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