"""Patch UI frame rates in .redswf files, keeping each file's size identical"""

from __future__ import annotations

import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from smoothui.build_common import fail
from smoothui.swf import (
    COMPRESSED_SIGS,
    HEADER_SIZE,
    SIGS,
    SPRITE_FIELDS_SIZE,
    Tag,
    is_color_transform_update,
    swf_get_header_offsets,
    swf_patch,
    valid_versions,
)
from smoothui.swf_tags import (
    END_TAG,
    SHOW_FRAME_TAG,
    TagCode,
    encode_tag_header,
    iter_tags,
)

# Note: terminology may be a bit off in this file; "movie" may be preferred in some of all places where "SWF" is used,
# but it really doesn't sound right to me.
#
# The frame rate is stored as an unsigned 8.8 fixed-point number
FPS_FIXED_POINT_SCALE = 256
MAX_RAW_FPS = 0xFFFF

@dataclass(frozen=True)
class FocusAnimation:
    """A DefineSprite that should be reduced to its first frame."""
    tag: Tag
    sprite_id: int
    frame_count: int
    tween_count: int
    first_frame_end: int    # offset just past the sprite's first ShowFrame tag




#
#
# TODO: tweak values prior to commit: tweens == frames (or possibly tweens == frames - 1, if the first frame doesn't count despite it's alpha transform)
# TODO: is stricter and most likely still matches 100%
#
#
# Focus animation patching constants
#
# A sprite could be is a focus animation if it has at least this many frames...
MIN_ANIMATED_FRAMES = 20
# ...and at least this many PlaceObject2 tags that change the color transform of an object
# already on the stage (this is how the alpha fades are encoded).
MIN_COLOR_TWEENS = 6

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
    if not data[pos + 3] in valid_versions:
        return None

    if sig not in COMPRESSED_SIGS:
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

def inspect_sprite(body: bytearray, tag: Tag) -> FocusAnimation | None:
    """Return a FocusAnimation if this DefineSprite should be frozen, otherwise None."""
    if tag.length < SPRITE_FIELDS_SIZE:
        fail(f"DefineSprite at offset {tag.start} is too short ({tag.length} bytes)")
    sprite_id, frame_count = struct.unpack_from("<HH", body, tag.payload_start)
    if frame_count < MIN_ANIMATED_FRAMES:
        return None

    tween_count = 0
    first_frame_end = None
    for inner in iter_tags(body, tag.payload_start + SPRITE_FIELDS_SIZE, tag.end):
        if inner.code == TagCode.FRAME_LABEL:
            # Labelled frames are jump targets for scripts (gotoAndPlay etc.), so removing
            # frames could break them. Leave such sprites alone.
            return None
        if inner.code == TagCode.PLACE_OBJECT2:
            if is_color_transform_update(body, inner):
                tween_count += 1
        elif inner.code == TagCode.SHOW_FRAME and first_frame_end is None:
            first_frame_end = inner.end

    if tween_count < MIN_COLOR_TWEENS:
        return None
    if first_frame_end is None:
        fail(f"DefineSprite {sprite_id} at offset {tag.start} has no ShowFrame tag")
    return FocusAnimation(tag, sprite_id, frame_count, tween_count, first_frame_end)

def find_focus_animations(body: bytearray) -> list[FocusAnimation]:
    """Scan the whole tag stream, validating it, without modifying anything."""
    _, first_tag_offset = swf_get_header_offsets(body)
    if first_tag_offset is None:
        fail("not a valid SWF body: cannot locate the tag stream")

    found = []
    for tag in iter_tags(body, first_tag_offset, len(body)):
        if tag.code == TagCode.DEFINE_SPRITE:
            animation = inspect_sprite(body, tag)
            if animation is not None:
                found.append(animation)
    return found

def freeze_sprite(body: bytearray, animation: FocusAnimation) -> None:
    """Rebuild the sprite as its first frame followed by empty frames.

    The frame count is preserved, so the sprite still reports and plays the same number
    of frames, but nothing changes after the first one."""
    tag = animation.tag
    first_frame = bytes(body[tag.payload_start:animation.first_frame_end])  # id, frame count, frame 1
    payload = first_frame + SHOW_FRAME_TAG * (animation.frame_count - 1) + END_TAG
    header = encode_tag_header(TagCode.DEFINE_SPRITE, len(payload), force_long=True)
    body[tag.start:tag.end] = header + payload

def freeze_focus_animations(body: bytearray) -> int:
    """Freeze every focus animation in the SWF body, in place. Returns how many were frozen.

    A focus animation is a DefineSprite with at least MIN_ANIMATED_FRAMES frames and at
    least MIN_COLOR_TWEENS color-transform updates (alpha fades), and no FrameLabel.
    Everything after its first frame is removed, which shrinks the body.

    The tag stream is fully validated before anything is modified; fails if it is malformed."""
    animations = find_focus_animations(body)
    for a in animations:
        print(f"[*] Frozen focus animation -> Sprite ID: {a.sprite_id} "
              f"(Frames: {a.frame_count}, Tweens: {a.tween_count})")

    # Rewrite back to front, so the offsets of the sprites still to be rewritten stay valid
    for a in reversed(animations):
        freeze_sprite(body, a)
    return len(animations)