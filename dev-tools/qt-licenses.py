"""Write the license texts that ship with Qt (PySide6) in the merger's build, from Qt's own sources (qtbase on
GitHub) at a version's tag: the LGPL-3.0 and GPL-3.0 texts, and the notices of the third-party code built into
Qt Core and Qt GUI (Qt Widgets has none), from qtbase's qt_attribution.json files. Run it again when
requirements_gui.txt's PySide6 version changes, and look over what it lists.

    python dev-tools/qt-licenses.py v6.12.0 assets/licenses/PySide6

PySide6's wheels hold only Qt's commercial license text, not the LGPL. The folder's README.txt is written by
hand and stays as it is.
"""
import argparse
import json
import re
import urllib.request
from pathlib import Path, PurePosixPath

REPO = "qt/qtbase"
MODULES = {"qtcore", "qtgui"}
# In Qt Core's and Qt GUI's lists, but not in what the merger ships: other platforms' code, and plugins that
# the spec leaves out (the JPEG reader), build tools
NOT_SHIPPED = {"QEventDispatcher on macOS", "forkfd", "Cocoa Platform Plugin", "DejaVu Fonts", "LibJPEG-turbo",
               "Native Style for Android", "X Server helper", "XCB-XInput", "Pixman", "Gradle wrapper"}


def fetch(url) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def source(tag, path) -> str:
    return fetch(f"https://raw.githubusercontent.com/{REPO}/{tag}/{path}").decode("utf-8")


def as_list(value):
    return [] if value is None else value if isinstance(value, list) else [value]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="e.g. v6.12.0")
    parser.add_argument("out", type=Path)
    args = parser.parse_args()

    tree = json.loads(fetch(f"https://api.github.com/repos/{REPO}/git/trees/{args.tag}?recursive=1"))
    assert not tree.get("truncated")
    paths = [entry["path"] for entry in tree["tree"]]
    entries = []
    for path in paths:
        if path.endswith("qt_attribution.json") and not path.startswith(("tests/", "src/testlib", "cmake/")):
            folder = PurePosixPath(path).parent
            data = json.loads(source(args.tag, path), strict=False)     # (some hold raw control characters)
            entries += [(folder, entry) for entry in as_list(data)]
    chosen = sorted(((folder, entry) for folder, entry in entries
                     if entry.get("QDocModule") in MODULES and entry["Name"] not in NOT_SHIPPED),
                    key=lambda item: item[1]["Name"].lower())
    found = {entry["Name"] for _, entry in entries}
    assert NOT_SHIPPED <= found, f"not in qtbase any more: {NOT_SHIPPED - found}"

    lines = [(f"Third-party code in Qt {args.tag.lstrip('v')} (Qt Core and Qt GUI), as Qt's sources list it "
              f"(qt_attribution.json in https://github.com/{REPO}/tree/{args.tag})."), ""]
    for folder, entry in chosen:
        lines += ["=" * 100, f"{entry['Name']}" + (f", version {entry['Version']}" if entry.get("Version") else "")]
        for key in ("QtUsage", "Homepage", "License"):
            if entry.get(key):
                lines.append(f"{key.removeprefix('Qt')}: {entry[key]}")
        copyright_lines = [line for text in as_list(entry.get("Copyright")) for line in text.splitlines()]
        copyright_lines += [line for name in as_list(entry.get("CopyrightFile"))
                            for line in source(args.tag, f"{folder}/{name}").splitlines()]
        if copyright_lines:
            lines += ["", *copyright_lines]
        files = [f"{folder}/{name}" for name in as_list(entry.get("LicenseFile")) + as_list(entry.get("LicenseFiles"))]
        if not files:   # Only an SPDX id: the text is in qtbase's LICENSES folder
            ids = [name for name in re.split(r"\s+(?:OR|AND|WITH)\s+|[()]", entry["LicenseId"]) if name.strip()]
            files = [f"LICENSES/{name.strip()}.txt" for name in ids]
        for path in files:
            lines += ["", "-" * 40 + f" {PurePosixPath(path).name}", source(args.tag, path).strip()]
        lines.append("")
        print(f"{entry['Name']}: {entry['LicenseId']} ({', '.join(files)})")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "Qt-third-party.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\r\n")
    for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt"):
        (args.out / name).write_text(source(args.tag, f"LICENSES/{name}"), encoding="utf-8", newline="\r\n")
    print(f"{len(chosen)} components written to {args.out}")


if __name__ == "__main__":
    main()
