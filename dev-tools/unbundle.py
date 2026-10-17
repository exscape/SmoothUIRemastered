#!/usr/bin/env python3

# unbundle.py -- list or extract the files in a Witcher 3 .bundle (e.g. content/content0/bundles/xml.bundle),
# without wcc_lite (see smoothui/witcher3/bundle.py). Extracted files are checked against their CRCs.
#
# Usage: python dev-tools/unbundle.py BUNDLE                      list the files (size, compression, path)
#        python dev-tools/unbundle.py BUNDLE --out DIR [FILTER...] extract the files whose path contains
#                                                                 any FILTER (all files if none), case-insensitively

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from smoothui.witcher3.bundle import BundleError, compression_name, extract, read_bundle


def main():
    ap = argparse.ArgumentParser(description="List or extract the files in a Witcher 3 .bundle")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("filters", nargs="*", help="extract only files whose path contains one of these")
    ap.add_argument("--out", type=Path, help="extract into this directory (otherwise: list)")
    args = ap.parse_intermixed_args()
    try:
        entries = read_bundle(args.bundle.read_bytes())
        if args.out is None:
            for e in entries:
                print(f"{e.size:>10}  {compression_name(e.compression):<6}  {e.path}")
            print(f"{len(entries)} files")
            return
        filters = [f.lower() for f in args.filters]
        wanted = [e for e in entries if not filters or any(f in e.path.lower() for f in filters)]
        written = extract(wanted, args.out)
    except BundleError as e:
        sys.exit(f"{args.bundle}: {e}")
    print(f"extracted {len(written)} files into {args.out}")


if __name__ == "__main__":
    main()
