"""Patch the UI in .redswf files, keeping each file's size identical"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ..common import print_substep_header, profile_key
from ..witcher3.redswf import replace_swf
from .dev_mode import dev_make_body_hook, resolve_dev_target
from .fixes import AFTER_RESCALE, BEFORE_RESCALE, apply_fixes
from .swf import (
    FPS_FIXED_POINT_SCALE,
    MAX_RAW_FPS,
    SWF_COMPRESSED_SIGS,
    SWF_HEADER_SIZE,
    SWF_SIGS,
    swf_get_header_offsets,
    swf_patch,
    valid_versions,
)
from .swf_patcher import (
    format_unmet_rule,
    report_settings,
    rescale_body,
)


@dataclass(frozen=True)
class PatchSettings:
    """How to patch one file; see config/profile_default.txt"""
    target_fps: float
    rules: tuple = ()       # SpeedRules ("speed"); the first matching one applies
    include: tuple = ()     # class name regexes and SpriteRefs
    exclude: tuple = ()
    fixes: tuple = ()       # names of fixes for bugs in the game's file (see fixes/)

def patch_files(src, dst, profile, *, dry_run=False, on_file=None):
    """Patch the given files (paths relative to src), writing changed ones to dst with the
    same relative path. profile maps each file's profile key to its PatchSettings; files that are
    not in it, or that are already at/above their target frame rate and have no fixes to apply, are
    not written.

    on_file(rel, status, report=None, error=None) is called as soon as each file's result is
    known: status is "patched" or "kept" (with the file's rescale report), or "failed" (with the
    error message).

    Returns (patched, kept, failed): patched holds {"path", "fps", "report"} dicts (fps: the file's
    frame rate now), kept holds {"path", "report"} dicts, and failed holds (rel, message) tuples.
    Raises NotADirectoryError / ValueError for invalid directories.
    """
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if not src.is_dir():
        raise NotADirectoryError(f"Not a directory: {src}")
    if dst == src or src in dst.parents:
        raise ValueError("Output dir must not be inside the input dir.")

    resolve_dev_target(profile)

    patched, kept, failed = [], [], []

    def report(rel, status, rescale_report=None, error=None):
        if on_file:
            on_file(rel, status, rescale_report, error)

    for key, settings in profile.items():
        rel = Path(key)
        orig = (src / rel).read_bytes()
        redswf_bytes = bytearray(orig)

        print_substep_header(rel.as_posix())
        try:
            assert 1 <= settings.target_fps <= 256
            rescale_report, new_swf_bytes = process(redswf_bytes, settings, dev_make_body_hook(rel))
        except ValueError as e:
            failed.append((rel, str(e)))
            report(rel, "failed", error=str(e))
            continue

        if new_swf_bytes is None:
            kept.append({"path": rel, "report": rescale_report})
            report(rel, "kept", rescale_report)
            continue

        if not dry_run:
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out_data = replace_swf(redswf_bytes, new_swf_bytes)
            out.write_bytes(out_data)

        fps = rescale_report["new_fps"] or rescale_report["old_fps"]    # not rescaled: only fixed
        patched.append({"path": rel, "fps": fps, "report": rescale_report})
        report(rel, "patched", rescale_report)

    return patched, kept, failed

class RuleIssues(NamedTuple):
    """Messages about explicit settings (speed, include, exclude, ...); see find_rule_issues"""
    errors: list[str]       # settings that were not followed: the output is NOT as configured
    warnings: list[str]     # inherited settings that matched in the release but no longer match
                            # anything: the output is as configured, but may need a look

def find_rule_issues(patched, kept, shared=(), recorded=None) -> RuleIssues:
    """Check explicit settings against the reports in patch_files' patched and kept lists.

    Errors: a setting that couldn't be followed (an unmet rule), or that matches no sprite in its
    file (for a fix: that changed nothing). Settings in shared (a profile's "*" block, as written
    there) only need to match in at least one file.

    For compat builds, recorded maps profile keys to the reports the Smooth UI release recorded.
    A setting from there that matches nothing in the other mod's version of a file is only a
    warning, and only if it matched something in the release (otherwise it is not reported)."""
    errors, warnings = [], []
    matched_anywhere = set()
    for entry in [*patched, *kept]:
        path, report = entry["path"], entry["report"]
        errors += [f"{path}: {format_unmet_rule(unmet)}" for unmet in report["unmet_rules"]]
        unmet = {unmet["rule"] for unmet in report["unmet_rules"]}   # already reported
        release = (recorded or {}).get(profile_key(path))
        inherited = dict(report_settings(release)) if release else {}
        # Why a fix changed nothing, in place of "matched no sprite"
        fix_details = {f"fix: {fix['name']}": f"{fix['result']}: {fix['detail']}" for fix in report.get("fixes", [])}
        for setting, matched in report_settings(report):
            if matched:
                matched_anywhere.add(setting)
            elif setting in unmet:
                continue
            elif setting in inherited:
                if inherited[setting]:
                    detail = f" ({fix_details[setting]})" if setting in fix_details else ""
                    warnings.append(f"{path}: {setting}: matched sprite(s) {inherited[setting]} in the "
                                    f"Smooth UI release, but nothing in this mod's version of the file{detail}")
            elif setting not in shared:
                errors.append(f"{path}: {setting}: {fix_details.get(setting, 'matched no sprite')}")
    errors += [f"* {setting}: matched no sprite in any file" for setting in shared
               if setting not in matched_anywhere]
    return RuleIssues(errors, warnings)

def process(redswf_bytes, settings, body_hook=None):
    """Find the SWF payload embedded in redswf_bytes, apply its fixes and rescale it.

    Returns (report, new_swf_bytes): the rescale report, and the new SWF (None if the file was left
    unchanged). Raises ValueError if there isn't exactly one valid SWF.
    """
    result = None
    new_swf_bytes = None
    for sig in SWF_SIGS:
        pos = redswf_bytes.find(sig)
        while pos != -1:
            payload = read_and_patch_swf(redswf_bytes, pos, sig, settings, body_hook)
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
        raise ValueError("found 0 payloads (expected 1); corrupt file?")
    return result, new_swf_bytes

def read_and_patch_swf(redswf_bytes, pos, sig, settings, body_hook=None):
    """Check for an SWF header at data[pos] and patch the payload if found."""
    if pos + SWF_HEADER_SIZE > len(redswf_bytes):
        return None
    if not redswf_bytes[pos + 3] in valid_versions:
        return None

    if sig not in SWF_COMPRESSED_SIGS:
        raise ValueError("SWF was not compressed: please open a GitHub issue that uncompressed SWFs needs to be supported!")

    return patch_fps(redswf_bytes, pos, settings, body_hook)

def patch_fps(redswf_bytes, pos, settings, body_hook=None):
    """Set the frame rate of the compressed SWF at redswf_bytes[pos] and interpolate animations,
    applying its fixes before or after that (see fixes/). body_hook (see dev_mode.py) runs on the result.

    Returns:
    None if there is no valid SWF stream at pos, or
    (stream_end, report, new_swf_bytes): report is the rescale report, with the fixes' records under
    "fixes" (see fixes.apply_fixes). report["new_fps"] is None if the frame rate was left unchanged;
    new_swf_bytes is None if the file was left unchanged entirely (no fix changed it either)."""

    def patch(body):
        """Helper that is called on the decompressed SWF bytes, and patches them in place"""
        # Most fixes go first, so the rescale (and its verification) works on the fixed file
        fixed, fix_records = apply_fixes(body, settings.fixes, BEFORE_RESCALE)
        if fixed is not None:
            body[:] = fixed
        result = rescale_body(body, settings.target_fps, rules=settings.rules, include=settings.include,
                              exclude=settings.exclude)
        if result.new_fps is not None:
            body[:] = result.body
        fixed_after, records_after = apply_fixes(body, settings.fixes, AFTER_RESCALE)
        if fixed_after is not None:
            body[:] = fixed_after
        fix_records = sorted(fix_records + records_after, key=lambda record: settings.fixes.index(record["name"]))
        changed = result.new_fps is not None or fixed is not None or fixed_after is not None
        return changed, {**result.report, "fixes": fix_records}

    return swf_patch(redswf_bytes, pos, patch, body_hook)

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