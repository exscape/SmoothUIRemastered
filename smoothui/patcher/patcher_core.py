"""Patch the UI in .redswf files, keeping each file's size identical"""

from __future__ import annotations

import hashlib
import logging
import struct
import traceback
from concurrent.futures import as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import NamedTuple

from ..common import SmoothUIError, fail, profile_key
from ..log import SUBHEADING
from ..parallel import make_pool, replay_records, run_logged, worker_count
from ..witcher3.bundle import BundleError, make_entry
from ..witcher3.redswf import CR2W_VERSION, RedSwf, RedSwfError, replace_swf, swf_sha256
from . import dev_mode
from .dev_mode import dev_make_body_hook, resolve_dev_target
from .fixes import AFTER_RESCALE, BEFORE_RESCALE, apply_fixes
from .swf import (
    FPS_FIXED_POINT_SCALE,
    MAX_RAW_FPS,
    SWF_COMPRESSED_SIGS,
    SWF_HEADER_SIZE,
    SWF_SIGS,
    MalformedSwfError,
    swf_get_header_offsets,
    swf_patch,
    valid_versions,
)
from .swf_patcher import (
    format_unmet_rule,
    report_settings,
    rescale_body,
)
from .swf_patcher.movie import RescaleError
from .swf_patcher.place_object import clear_parse_cache

log = logging.getLogger(__name__)

# The errors the patcher raises on purpose, whose message says what is wrong with the file. Anything else
# (a plain ValueError too) may well be a bug, so its traceback goes into the failure message.
EXPECTED_ERRORS = (SmoothUIError, RescaleError, RedSwfError)

# The only movie that loads others into its ActionScript class space is hud.redswf, which loads the HUD
# modules from this folder: elsewhere, the classes a file lacks are only Flash's own
SHARED_CLASSES_FOLDER = "gameplay/gui_new/swf/hud/"

@dataclass(frozen=True, slots=True)
class PatchSettings:
    """How to patch one file; see config/profile_default.txt"""
    target_fps: float
    rules: tuple = ()       # SpeedRules ("speed"); the first matching one applies
    include: tuple = ()     # class name regexes and SpriteRefs
    exclude: tuple = ()
    fixes: tuple = ()       # names of fixes for bugs in the game's file (see fixes/)
    source_fps: float | None = None     # the frame rate it plays at in game, where that isn't its header's
                                        # ("source fps"; e.g. HUD modules play at the root HUD movie's rate)

def patch_files(inputs, profile, *, on_file=None, pool=None):
    """Patch the given files (SourceFiles) in memory, each with its settings in profile ({profile key:
    PatchSettings}).

    The files are patched in parallel, in pool if given (see parallel.make_pool), else in a pool made for
    this call. They are patched in-process instead when there is only one file or one worker (see
    parallel.worker_count), or a dev hook is active (see dev_mode.py). Either way, each file's log output
    comes in one piece, as soon as the file is done (so the order of the files can differ between runs).

    on_file(path, status, report=None, error=None) is called as soon as each file's result is
    known, right after its log output: status is "patched" or "kept" (with the file's rescale report),
    or "failed" (with the error message).

    Returns (patched, kept, failed), in input order, lists of dicts with the file's "path" and "origin" (as in its
    SourceFile). patched ones also hold "entry" (the patched file, compressed, as a BundleEntry for
    write_bundle), "hashes" (sha256, swf_sha256 and size of the source and output files, and the output's
    CRC32, as manifests record them: source_sha256, output_swf_sha256, output_crc32, ...), "fps" (its frame rate now) and "report"; kept
    ones (already at or above their target frame rate, with no fixes to apply) hold "report"; failed ones
    hold "error" (the message).

    A file fails on any exception: input the patcher can't read, doesn't support or misunderstands
    (e.g. a failed verification), or a bug. The other files are still patched, so that all problems
    are reported, but the caller must treat any failure as a failure of the whole mod: never leave a
    failed file out and ship the rest.
    """
    resolve_dev_target(profile)
    jobs = [(source, profile[source.path]) for source in inputs]

    results = [None] * len(jobs)
    for i, result in _patch_jobs(jobs, pool):
        results[i] = result
        if on_file:
            on_file(result["path"], result["status"], result.get("report"), result.get("error"))
    patched, kept, failed = [], [], []
    for result in results:
        status = result.pop("status")
        {"patched": patched, "kept": kept, "failed": failed}[status].append(result)
    return patched, kept, failed

def _patch_jobs(jobs, pool):
    """(index, _patch_file's result) for each (SourceFile, PatchSettings) in jobs, as each is done"""
    if dev_mode.DEV_MODE or worker_count(len(jobs)) == 1:
        for i, (source, settings) in enumerate(jobs):
            yield i, _patch_file(source, settings, dev_make_body_hook(source.path))
    elif pool is None:
        with make_pool(worker_count(len(jobs))) as own_pool:
            yield from _patch_in_pool(own_pool, jobs)
    else:
        yield from _patch_in_pool(pool, jobs)

def _patch_in_pool(pool, jobs):
    # The largest files first, so that a slow one starts at once instead of waiting for a free worker
    order = sorted(range(len(jobs)), key=lambda i: -jobs[i][0].size)
    futures = {pool.submit(run_logged, _patch_file, *jobs[i]): i for i in order}
    try:
        for future in as_completed(futures):
            result, records = future.result()
            replay_records(records)
            yield futures[future], result
    except BrokenProcessPool as e:
        fail(f"a worker process stopped while patching files: {e}")
    finally:
        for future in futures:
            future.cancel()

def _patch_file(source, settings, body_hook=None):
    """Patch one file; returns its result for patch_files, with its "status" """
    result = {"path": source.path, "origin": source.origin}
    log.info(source.path, extra=SUBHEADING)
    try:
        data = source.read()
    except (OSError, BundleError) as e:
        return {**result, "status": "failed", "error": f"can't read it from {source.file}: {e}"}

    # Most of a .redswf's data is textures, which pass through unchanged: only the SWF is patched, and
    # the container's data is copied as few times as possible (big files are 10-20 MiB)
    try:
        assert 1 <= settings.target_fps <= 256
        redswf = RedSwf(data)
        if "header file size" in redswf.stale:
            log.warning(f"The .redswf's header states a file size of {redswf.file_size} bytes, but the file has "
                        f"{len(data)}; this is ignored")
        if redswf.version != CR2W_VERSION:
            log.info(f"The .redswf is CR2W version {redswf.version}: it is written as version {CR2W_VERSION}")
        shares_classes = source.path.startswith(SHARED_CLASSES_FOLDER)
        rescale_report, new_swf_bytes = process(redswf.swf, settings, body_hook, shares_classes=shares_classes)
        del redswf
        if new_swf_bytes is None:
            return {**result, "status": "kept", "report": rescale_report}
        out = replace_swf(data, new_swf_bytes)
        hashes = {"source_sha256": hashlib.sha256(data).hexdigest(), "output_sha256": hashlib.sha256(out).hexdigest(),
                  "source_swf_sha256": swf_sha256(data), "output_swf_sha256": swf_sha256(out),
                  "source_size": len(data), "output_size": len(out)}
        del data
        entry = make_entry(source.path, out)
        # As the bundle's table of contents records it, so an installed build can be identified without reading it
        hashes["output_crc32"] = entry.crc
    except Exception as e:  # noqa: BLE001 (see patch_files: the caller fails the mod)
        return {**result, "status": "failed", "error": _failure_message(e)}
    finally:
        clear_parse_cache()

    fps = rescale_report["new_fps"] or rescale_report["old_fps"]    # not rescaled: only fixed
    return {**result, "status": "patched", "entry": entry, "hashes": hashes, "fps": fps, "report": rescale_report}

def _failure_message(e: Exception) -> str:
    if isinstance(e, EXPECTED_ERRORS):
        return str(e)
    trace = "".join(traceback.format_exception(type(e), e, e.__traceback__)).rstrip()
    return f"unexpected {type(e).__name__}: {e} (a bug, or a file the patcher doesn't understand)\n{trace}"

class RuleIssue(NamedTuple):
    """A message about one explicit setting; str() gives it with its file"""
    path: str | None        # the file, as patch_files names it; None for a setting of a profile's "*" block
    message: str            # starting with the setting, as written in a profile
    fix: str | None = None  # the fix's name, if the setting is a fix

    def __str__(self):
        return f"{self.path}: {self.message}" if self.path is not None else f"* {self.message}"

class RuleIssues(NamedTuple):
    """Issues with explicit settings (speed, include, exclude, ...); see find_rule_issues"""
    errors: list[RuleIssue]     # settings that were not followed: the output is NOT as configured
    warnings: list[RuleIssue]   # inherited settings that matched in the release but no longer match
                                # anything: the output is as configured, but may need a look

def find_rule_issues(patched, kept, shared=(), recorded=None) -> RuleIssues:
    """Check explicit settings against the reports in patch_files' patched and kept lists.

    Errors: a setting that couldn't be followed (an unmet rule), or that matches no sprite in its
    file (for a fix: that changed nothing). Settings in shared (a profile's "*" block, as written
    there) only need to match in at least one file.

    For compat builds, recorded maps profile keys to the reports the Smooth UI release recorded.
    A setting from there that matches nothing in the other mod's version of a file is only a
    warning, and only if it matched something in the release (otherwise it is not reported). A file that
    wasn't rescaled (already at or above the target frame rate, so only fixed) has no use for the rescale's
    settings (speed rules, include, exclude), so those are not reported for it: only its fixes are."""
    errors, warnings = [], []
    matched_anywhere = set()
    for entry in [*patched, *kept]:
        path, report = entry["path"], entry["report"]
        errors += [RuleIssue(path, format_unmet_rule(unmet)) for unmet in report["unmet_rules"]]
        unmet = {unmet["rule"] for unmet in report["unmet_rules"]}   # already reported
        not_rescaled = "new_fps" in report and report["new_fps"] is None
        release = (recorded or {}).get(profile_key(path))
        inherited = dict(report_settings(release)) if release else {}
        # Why a fix changed nothing, in place of "matched no sprite"
        fixes = {f"fix: {fix['name']}": fix for fix in report.get("fixes", [])}
        fix_details = {setting: f"{fix['result']}: {fix['detail']}" for setting, fix in fixes.items()}
        for setting, matched in report_settings(report):
            fix = fixes[setting]["name"] if setting in fixes else None
            if matched:
                matched_anywhere.add(setting)
            elif setting in unmet:
                continue
            elif setting in inherited:
                if inherited[setting] and (fix or not not_rescaled):
                    detail = f" ({fix_details[setting]})" if setting in fix_details else ""
                    warnings.append(RuleIssue(path, f"{setting}: matched sprite(s) {inherited[setting]} in the "
                                              f"Smooth UI release, but nothing in this mod's version of the file{detail}",
                                              fix))
            elif setting not in shared:
                errors.append(RuleIssue(path, f"{setting}: {fix_details.get(setting, 'matched no sprite')}", fix))
    errors += [RuleIssue(None, f"{setting}: matched no sprite in any file") for setting in shared
               if setting not in matched_anywhere]
    return RuleIssues(errors, warnings)

def process(data, settings, body_hook=None, *, shares_classes=True):
    """Find the SWF payload in data, apply its fixes and rescale it. data is normally the SWF taken from
    a .redswf (RedSwf(...).swf); a whole .redswf works too, but more slowly, as it is searched for the SWF.
    shares_classes: whether other movies may share its ActionScript classes (see RescaleOptions).

    Returns (report, new_swf_bytes): the rescale report, and the new SWF (None if the file was left
    unchanged). Raises MalformedSwfError unless there is exactly one SWF it can read, and RescaleError
    (or anything else; see patch_files) if it can't be patched.
    """
    result = None
    new_swf_bytes = None
    for sig in SWF_SIGS:
        pos = data.find(sig)
        while pos != -1:
            payload = read_and_patch_swf(data, pos, sig, settings, body_hook, shares_classes)
            if payload is None:
                pos = data.find(sig, pos + len(sig))
                continue

            end, found, returned_bytes = payload
            if found is not None:
                if result is not None:
                    raise MalformedSwfError("found more than 1 payload (expected 1)")
                result = found
                new_swf_bytes = returned_bytes
            pos = data.find(sig, end)

    if result is None:
        raise MalformedSwfError("found 0 payloads (expected 1); corrupt file?")
    return result, new_swf_bytes

def read_and_patch_swf(data, pos, sig, settings, body_hook=None, shares_classes=True):
    """Check for an SWF header at data[pos] and patch the payload if found."""
    if pos + SWF_HEADER_SIZE > len(data):
        return None
    if not data[pos + 3] in valid_versions:
        return None

    if sig not in SWF_COMPRESSED_SIGS:
        raise MalformedSwfError("SWF was not compressed: please contact me (GitHub/Nexus Mods) about this and I'll add support.")
    return patch_fps(data, pos, settings, body_hook, shares_classes)

def patch_fps(data, pos, settings, body_hook=None, shares_classes=True):
    """Set the frame rate of the compressed SWF at data[pos] and interpolate animations,
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
        # Rescale from the rate it really plays at, keeping its speed in game
        if settings.source_fps is not None and patch_fps_in_body(body, settings.source_fps) is None:
            raise MalformedSwfError("can't read the frame rate in its header")
        result = rescale_body(body, settings.target_fps, rules=settings.rules, include=settings.include,
                              exclude=settings.exclude, shares_classes=shares_classes)
        if result.new_fps is not None:
            body[:] = result.body
        fixed_after, records_after = apply_fixes(body, settings.fixes, AFTER_RESCALE)
        if fixed_after is not None:
            body[:] = fixed_after
        fix_records = sorted(fix_records + records_after, key=lambda record: settings.fixes.index(record["name"]))
        changed = result.new_fps is not None or fixed is not None or fixed_after is not None
        report = {**result.report, "fixes": fix_records}
        if settings.source_fps is not None:
            report["source_fps"] = settings.source_fps     # old_fps is then this, not the header's
        return changed, report

    return swf_patch(data, pos, patch, body_hook)

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