"""Patch the UI in .redswf files, keeping each file's size identical"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ..common import profile_key
from ..swf import (
    FPS_FIXED_POINT_SCALE,
    MAX_RAW_FPS,
    SWF_COMPRESSED_SIGS,
    SWF_HEADER_SIZE,
    SWF_SIGS,
    swf_get_header_offsets,
    swf_patch,
    valid_versions,
)
from .dev_mode import dev_make_body_hook, resolve_dev_target
from .redswf import replace_swf
from .swf_patcher import format_unmet_rule, make_rules, report_to_text, rescale_body


@dataclass(frozen=True)
class PatchSettings:
    target_fps: float
    rules: tuple = ()   # FpsRules, applied to every file

def file_target_fps(profile, rel):
    """The frame rate the profile states for this file"""
    target = profile.get(profile_key(rel))
    assert target is not None
    return float(target)

def patch_files(src, dst, profile, *, rules=(), dry_run=False, on_file=None):
    """Patch the given files (paths relative to src), writing changed ones to dst with the
    same relative path. profile maps each file to its target frame rate; files that are not in
    it, or that are already at/above that rate, are not written.

    rules are FpsRules or (class name regex, fps) pairs, applied to every file.

    on_file(rel, status, old_fps, new_fps, note) is called as soon as each file's result is
    known; status is "patched", "kept" or "failed".

    Returns (patched, kept, failed, rule_problems): lists of relative paths, except that failed
    holds (rel, message) tuples, and rule_problems holds messages about explicit rules that were
    not followed (including rules that matched nothing at all). Files are still patched as well as
    possible when a rule can't be followed, so the caller must make rule_problems very visible.
    Raises NotADirectoryError / ValueError for invalid directories.
    """
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if not src.is_dir():
        raise NotADirectoryError(f"Not a directory: {src}")
    if dst == src or src in dst.parents:
        raise ValueError("Output dir must not be inside the input dir.")

    resolve_dev_target(profile)

    rules = make_rules(rules)
    patched, kept, failed, rule_problems = [], [], [], []
    matched_rules = set()   # indices into rules

    def report(rel, status, old=None, new=None, note=""):
        if on_file:
            on_file(rel, status, old, new, note)

    for rel in map(Path, profile.keys()):
        orig = (src / rel).read_bytes()
        redswf_bytes = bytearray(orig)

        # TODO: used by the temp redswf code which need refactoring to fit into the project
        orig_redswf_bytes = redswf_bytes[:]

        try:
            target_fps = file_target_fps(profile, rel)
            assert target_fps >= 1 and target_fps <= 256
            print(f"********** START PROCESSING {rel} ************")

            # TODO
            settings = PatchSettings(target_fps, rules)
            result, new_swf_bytes = process(redswf_bytes, settings, dev_make_body_hook(rel))
        except ValueError as e:
            failed.append((rel, str(e)))
            report(rel, "failed", note=str(e))
            continue

        old_fps, new_fps, note, rescale_report = result
        rule_problems += [f"{rel}: {format_unmet_rule(unmet)}" for unmet in rescale_report["unmet_rules"]]
        matched_rules.update(i for i, rule in enumerate(rescale_report.get("rules", [])) if rule["matched"])
        if new_fps is None:
            kept.append(rel)
            report(rel, "kept", old_fps, note=note)
            continue

        if not dry_run:
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)

            # TODO: replace when extent-supporting patcher is fully integrated
            print("*** Got to container work ***")

            print(new_swf_bytes[:32])




# TODO: re-enable validate!

            out_data = replace_swf(orig_redswf_bytes, new_swf_bytes)
            out.write_bytes(out_data)

        patched.append({"path": rel, "fps": new_fps})
        report(rel, "patched", old_fps, new_fps, note)
        print(f"********** DONE PROCESSING {rel} ************\n")

    rule_problems += [f"{rule.description}: matched no sprite in any of the patched files"
                      for i, rule in enumerate(rules) if i not in matched_rules]
    return patched, kept, failed, rule_problems

def process(redswf_bytes, settings, rel):
    """Find the SWF payload embedded in data and set its frame rate. Patches data in place.

    Returns (old_fps, new_fps, note, report). Raises ValueError if there isn't exactly one valid SWF.
    """
    result = None
    new_swf_bytes = None
    for sig in SWF_SIGS:
        pos = redswf_bytes.find(sig)
        while pos != -1:
            print(f"read_and_patch at pos {pos}: {sig}")
            payload = read_and_patch_swf(redswf_bytes, pos, sig, settings, rel)
            if payload is None:
                pos = redswf_bytes.find(sig, pos + len(sig))
                continue

            end, found, returned_bytes = payload
            if found is not None:
                if result is not None:
                    raise ValueError("found more than 1 payload (expected 1)")
                result = found
                new_swf_bytes = returned_bytes
            pos = redswf_bytes.find(sig, end)

    if result is None:
        #raise ValueError("found 0 payloads (expected 1)")

        # TEMP
        from ..common import fail
        fail("0 payloads found in process")
    return result, new_swf_bytes

def read_and_patch_swf(redswf_bytes, pos, sig, settings, rel):
    """Check for an SWF header at data[pos] and patch the payload if found."""
    if pos + SWF_HEADER_SIZE > len(redswf_bytes):
        return None
    if not redswf_bytes[pos + 3] in valid_versions:
        return None

    if sig not in SWF_COMPRESSED_SIGS:
        raise ValueError("SWF was not compressed: please open a GitHub issue that uncompressed SWFs needs to be supported!")

    return patch_fps(redswf_bytes, pos, settings, rel)

def patch_fps(redswf_bytes, pos, settings, body_hook=None):
    """Set the frame rate of the compressed SWF at redswf_bytes[pos] and interpolate animations.

    Returns:
    None if there is no valid SWF stream at pos
    (stream_end, found) where found is None if the body doesn't look like an SWF, or
    (old_fps, new_fps_or_None, note, report), report being the rescale report dict.
    new_fps is None only when nothing at all changed;
    if just the freeze changed something, new_fps equals old_fps, so the file still
    counts as patched and gets written."""

    def patch(body):
        """Helper that is called on the decompressed SWF bytes, and patches them in place"""
        result = rescale_body(body, settings.target_fps, rules=settings.rules, exclude=("InputFeedbackButton",))
        print(f"\n{report_to_text(result.report)}\n")
        if result.new_fps is None:
            return False, (result.old_fps, None, None, result.report)
        body[:] = result.body
        return True, (result.old_fps, result.new_fps, None, result.report)

    out = swf_patch(redswf_bytes, pos, patch, body_hook)

    if out is None:
        print("!!! out is None")
        return None
    stream_end, result, padding, new_swf_bytes = out
    if result is None:
        print("!!! result is None")
        return stream_end, None, None
    old_fps, applied_fps, note, report = result
    if padding is not None:
        note = f"{note}, {padding} pad bytes" if note else f"{padding} pad bytes"
    return stream_end, (old_fps, applied_fps, note, report), new_swf_bytes

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