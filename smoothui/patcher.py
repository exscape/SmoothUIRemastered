"""Patch UI frame rates in .redswf files, keeping each file's size identical.

This is a library; run build-mod.py to build the mod.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from zopfli.zlib import compress as zopfli_compress

SIGS = (b"GFX", b"FWS", b"CFX", b"CWS")

@dataclass(frozen=True)
class PatchSettings:
    """Exactly one of fps / multiplier must be given"""
    fps: float | None = None            # set this exact frame rate
    multiplier: float | None = None     # multiply the original rate
    max_fps: float = 120                # clamp the target rate to this
    skip_at_or_above: float = 60        # leave files already at/above this rate alone

    def __post_init__(self):
        if (self.fps is None) == (self.multiplier is None):
            raise ValueError("specify exactly one of fps or multiplier")

def target_fps(orig, s):
    """Return the new fps, or None to leave the file untouched"""
    if orig >= s.skip_at_or_above:
        return None
    t = s.fps if s.fps is not None else orig * s.multiplier
    t = min(t, s.max_fps, 255.99)
    return t if t > orig else None

def patch_body(body, s):
    """body = decompressed data after the 8-byte header. Patches in place.
    Returns None if not a valid movie, else (old_fps, new_fps_or_None)"""
    nbits = body[0] >> 3
    if nbits == 0:
        return None
    offset = (5 + 4 * nbits + 7) // 8
    if offset + 4 > len(body):
        return None
    raw, frames = struct.unpack_from("<HH", body, offset)
    if raw == 0 or frames == 0:
        return None
    old = raw / 256.0
    new = target_fps(old, s)
    if new is None:
        return old, None
    new_raw = round(new * 256)
    if not 1 <= new_raw <= 0xFFFF:
        raise ValueError(f"fps {new:.2f} out of range (max 255.99)")
    struct.pack_into("<H", body, offset, new_raw)
    return old, new_raw / 256.0

def recompress(body, max_len):
    """Recompress the patched data. Note that the output is zero padded, so we don't need this to
    be as small as possible; just small enough to fit into the existing container"""
    raw = bytes(body)
    smallest = None

    # try zlib first
    # 8 compresses slightly better(?!) and often yields the exact size of the original blob,
    # so let's start there
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

def process(data, s):
    """Returns list of (old, new_or_None, note) per valid payload; patches data in place"""
    found = []
    for sig in SIGS:
        pos = data.find(sig)
        while pos != -1:
            if pos + 8 <= len(data) and 1 <= data[pos + 3] <= 40:
                (length,) = struct.unpack_from("<I", data, pos + 4)
                compressed = sig in (b"CFX", b"CWS")
                if compressed:
                    d = zlib.decompressobj()
                    try:
                        body = bytearray(d.decompress(bytes(data[pos + 8:])))
                    except zlib.error:
                        body = None

                    if body is not None and d.eof:
                        clen = len(data) - (pos + 8) - len(d.unused_data)
                        res = patch_body(body, s)
                        if res and res[1] is None:
                            found.append((res[0], None, "left alone"))
                        elif res:
                            out = recompress(body, clen)
                            pad = clen - len(out)
                            data[pos + 8:pos + 8 + clen] = out + b"\0" * pad
                            found.append((*res, f"compressed, {pad} pad bytes"))
                        pos += 8 + clen
                        pos = data.find(sig, pos)
                        continue
                elif 21 <= length and pos + length <= len(data):
                    body = bytearray(data[pos + 8:pos + length])
                    res = patch_body(body, s)
                    if res and res[1] is None:
                        found.append((res[0], None, "left alone"))
                    elif res:
                        data[pos + 8:pos + length] = body
                        found.append((*res, "uncompressed"))
                    pos += length
                    pos = data.find(sig, pos)
                    continue
            pos = data.find(sig, pos + 3)
    return found

def patch_files(src, dst, rels, settings, *, dry_run=False, on_file=None):
    """Patch the given files (paths relative to src), writing changed ones to dst with the
    same relative path. Files that are left alone by the rate rules, or that fail, are not written.

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

    patched, kept, failed = [], [], []

    def report(rel, status, old=None, new=None, note=""):
        if on_file:
            on_file(rel, status, old, new, note)

    for rel in map(Path, rels):
        orig = (src / rel).read_bytes()
        data = bytearray(orig)
        try:
            found = process(data, settings)
        except ValueError as e:
            failed.append((rel, str(e)))
            report(rel, "failed", note=str(e))
            continue
        if len(found) != 1 or len(data) != len(orig):
            msg = f"found {len(found)} payloads (expected 1)"
            failed.append((rel, msg))
            report(rel, "failed", note=msg)
            continue

        old, new, note = found[0]
        if new is None:
            kept.append(rel)
            report(rel, "kept", old, note=note)
            continue

        if not dry_run:
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        patched.append(rel)
        report(rel, "patched", old, new, note)

    return patched, kept, failed