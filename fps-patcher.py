#!/usr/bin/env python3

# Automatically patch UI frame rates (for non-excluded files).
# Note: you should probably run build-mod.py instead.

import argparse
import struct
import sys
import zlib
from pathlib import Path

from zopfli.zlib import compress as zopfli_compress

SIGS = (b"GFX", b"FWS", b"CFX", b"CWS")
DEFAULT_EXCLUDE_FILE = Path(__file__).resolve().parent / "excluded-paths.txt"

def load_exclusions(path):
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        e = line.replace("\\", "/").lower()
        while e.startswith("./"):
            e = e[2:]
        e = e.strip("/")
        if e:
            entries.append(e)
    return entries

def match_exclusion(rel_posix_lower, entries):
    """Return the matching entry (file path or directory prefix), or None."""
    for e in entries:
        if rel_posix_lower == e or rel_posix_lower.startswith(e + "/"):
            return e
    return None

def target_rate(orig, a):
    """Return the new fps, or None to leave the file untouched."""
    if orig >= a.skip_at_or_above:
        return None
    t = a.fps if a.fps is not None else orig * a.multiplier
    t = min(t, a.max_fps, 255.99)
    return t if t > orig else None

def patch_body(body, a):
    """body = decompressed data after the 8-byte header. Patches in place.
    Returns None if not a valid movie, else (old_fps, new_fps_or_None)."""
    nbits = body[0] >> 3
    if nbits == 0 or nbits > 31:
        return None
    offset = (5 + 4 * nbits + 7) // 8
    if offset + 4 > len(body):
        return None
    raw, frames = struct.unpack_from("<HH", body, offset)
    if raw == 0 or frames == 0:
        return None
    old = raw / 256.0
    new = target_rate(old, a)
    if new is None:
        return old, None
    new_raw = round(new * 256)
    if not 1 <= new_raw <= 0xFFFF:
        raise ValueError(f"fps {new:.2f} out of range (max 255.99)")
    struct.pack_into("<H", body, offset, new_raw)
    return old, new_raw / 256.0

def recompress(body, max_len):
    raw = bytes(body)
    smallest = None

    # try zlib first
    for mem in (9, 8):
        c = zlib.compressobj(9, zlib.DEFLATED, 15, mem)
        out = c.compress(raw) + c.flush()
        smallest = len(out) if smallest is None else min(smallest, len(out))
        if len(out) <= max_len:
            return out

    # zlib failed, output too large: try zopfli with increasing effort
    # This is slower, but is needed for gwent_game at least in my tests so far
    for iters in (15, 100):
        out = zopfli_compress(raw, numiterations=iters)
        smallest = min(smallest, len(out))
        if len(out) <= max_len:
            assert zlib.decompress(out) == raw
            return out

    raise ValueError(f"recompressed stream doesn't fit "
                     f"(smallest {smallest} bytes vs {max_len} allowed)")

def process(data, a):
    """Returns list of (old, new_or_None, note) per valid payload; patches data in place."""
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
                        res = patch_body(body, a)
                        if res and res[1] is None:
                            found.append((res[0], None, "left alone"))
                        elif res:
                            out = recompress(body, clen)
                            pad = clen - len(out)
                            data[pos + 8:pos + 8 + clen] = out + b"\0" * pad
                            found.append((*res, f"compressed, {pad} pad bytes"))
                elif 21 <= length and pos + length <= len(data):
                    body = bytearray(data[pos + 8:pos + length])
                    res = patch_body(body, a)
                    if res and res[1] is None:
                        found.append((res[0], None, "left alone"))
                    elif res:
                        data[pos + 8:pos + length] = body
                        found.append((*res, "uncompressed"))
            pos = data.find(sig, pos + 3)
    return found

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_dir", type=Path)
    ap.add_argument("output_dir", type=Path)

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fps", type=float)
    g.add_argument("--multiplier", type=float)

    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-fps", type=float, default=120,
                    help="clamp the target rate to this (default 120)")
    ap.add_argument("--skip-at-or-above", type=float, default=60,
                    help="leave files already at/above this rate alone (default 60)")
    ap.add_argument("--exclude-file", type=Path, default=None,
                    help=f"list of dirs/files to skip (default: {DEFAULT_EXCLUDE_FILE.name} "
                         "next to this script, if present)")
    a = ap.parse_args()

    src, dst = a.input_dir.resolve(), a.output_dir.resolve()
    if not src.is_dir():
        sys.exit(f"Not a directory: {src}")
    if dst == src or src in dst.parents:
        sys.exit("Output dir must not be inside the input dir.")

    exclusions = []
    ex_path = a.exclude_file or DEFAULT_EXCLUDE_FILE
    if ex_path.is_file():
        exclusions = load_exclusions(ex_path)
        print(f"Loaded {len(exclusions)} exclusion entries from {ex_path}")
    elif a.exclude_file:
        sys.exit(f"Exclude file not found: {ex_path}")
    used = set()

    ok = kept = excluded = bad = 0
    for f in sorted(src.rglob("*.redswf")):
        rel = f.relative_to(src)
        hit = match_exclusion(rel.as_posix().lower(), exclusions)
        if hit:
            used.add(hit)
            # print(f"EXCL {rel} (matches '{hit}')")
            excluded += 1
            continue

        orig = f.read_bytes()
        data = bytearray(orig)
        try:
            res = process(data, a)
        except ValueError as e:
            print(f"SKIP {rel}: {e}", file=sys.stderr); bad += 1; continue
        if len(res) != 1 or len(data) != len(orig):
            print(f"SKIP {rel}: found {len(res)} payloads (expected 1)", file=sys.stderr)
            bad += 1; continue
        old, new, note = res[0]
        if new is None:
            print(f"KEEP {rel}: {old:g} fps (not changed)")
            kept += 1
            continue
        print(f"{rel}: {old:g} -> {new:g} fps ({note})")
        if not a.dry_run:
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        ok += 1

    for e in exclusions:
        if e not in used:
            print(f"WARNING: exclusion '{e}' matched no files", file=sys.stderr)
    print()
    print(f"Done: {ok} patched, {kept} left alone, {excluded} excluded, {bad} skipped.")

if __name__ == "__main__":
    main()
