#!/usr/bin/env python3

"""apply-fixes.py -- apply fixes from smoothui/patcher/fixes/ to an SWF file, outside of a build.

Builds apply the fixes a profile names (fix: ...) on their own; this is for experimenting, e.g. on a
file dumped with the build's dev hooks. Reads an SWF (GFX/FWS, or compressed CFX/CWS) and writes it
uncompressed. OUT may be the same file as IN. Nothing is rescaled here, so the fixes that run before
and after the rescale in builds run one after the other.

    python dev-tools/apply-fixes.py gwint_game_payload.swf gwint_game_payload.swf --fix gwent-3d-bug-fix
"""

import argparse
import struct
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from smoothui.patcher.fixes import AFTER_RESCALE, BEFORE_RESCALE, FIXES, apply_fixes
from smoothui.patcher.swf import (
    SWF_COMPRESSED_SIGS,
    SWF_HEADER_SIZE,
    SWF_UNCOMPRESSED_SIGS,
)
from smoothui.patcher.swf_patcher import format_fix


def main():
    ap = argparse.ArgumentParser(description="Apply fixes from smoothui/patcher/fixes/ to an SWF file")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--fix", action="append", choices=FIXES, required=True, help="a fix to apply (repeatable)")
    args = ap.parse_args()

    data = Path(args.input).read_bytes()
    sig, version = data[:3], data[3:4]
    body = data[SWF_HEADER_SIZE:]
    if sig in SWF_COMPRESSED_SIGS:
        body = zlib.decompress(body)
        sig = SWF_UNCOMPRESSED_SIGS[sig]
    elif sig not in SWF_UNCOMPRESSED_SIGS.values():
        sys.exit(f"{args.input}: not a SWF")
    body = body[:struct.unpack_from("<I", data, 4)[0] - SWF_HEADER_SIZE]

    names, fixed, records = list(dict.fromkeys(args.fix)), body, []
    for phase in (BEFORE_RESCALE, AFTER_RESCALE):
        new, phase_records = apply_fixes(fixed, names, phase)
        fixed = fixed if new is None else new
        records += phase_records
    print("\n".join(format_fix(record) for record in records))
    if fixed is body:
        sys.exit("Nothing changed; not writing a file")
    Path(args.output).write_bytes(sig + version + struct.pack("<I", SWF_HEADER_SIZE + len(fixed)) + fixed)
    print(f"Wrote {args.output}: body {len(body)} -> {len(fixed)} bytes")


if __name__ == "__main__":
    main()
