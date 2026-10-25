"""Zip a built tools folder (build-tools.sh --zip) as a release ships it: SmoothUITools-X.Y.Z.zip, with the
folder "Smooth UI Tools" at its root, and no data\\ (the user's own).

    make-tools-zip.py BUILT_DIR OUT.zip
"""

import sys
import zipfile
from pathlib import Path

ROOT_FOLDER = "Smooth UI Tools"
KEPT_OUT = ("data",)


def main(built: Path, out: Path):
    files = sorted(p for p in built.rglob("*") if p.is_file() and p.relative_to(built).parts[0] not in KEPT_OUT)
    if not (built / "SmoothUIMerger.exe").exists():
        raise SystemExit(f"error: {built} holds no SmoothUIMerger.exe")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files:
            z.write(path, f"{ROOT_FOLDER}/{path.relative_to(built).as_posix()}")
    print(f"Wrote {out} ({out.stat().st_size / 1024 / 1024:.1f} MiB, {len(files)} files)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
