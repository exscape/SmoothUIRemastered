"""Write a tools release's notes (the merger's "What's new" text) from CHANGELOG_tools.md, or fail if they're
missing: run first by .github/workflows/release-tools.yml, and before tagging.

    release-notes.py X.Y.Z OUT.md

Checks that merger_version in config/config.toml is X.Y.Z, and that CHANGELOG_tools.md's first section is
"# vX.Y.Z" and not empty. OUT.md gets that section without its heading: the merger adds its own.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG_tools.md"
CONFIG = ROOT / "config" / "config.toml"


def main(version: str, out: Path):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"error: the version must be X.Y.Z, not {version!r}")
    with open(CONFIG, "rb") as f:
        configured = tomllib.load(f).get("versions", {}).get("merger_version")
    if configured != version:
        raise SystemExit(f"error: merger_version in {CONFIG.name} is {configured!r}, not {version!r}")
    if not CHANGELOG.is_file():
        raise SystemExit(f"error: no {CHANGELOG.name}")
    # The sections: "# vX.Y.Z" lines, each followed by its text
    sections = re.split(r"^# (.*)$", CHANGELOG.read_text(encoding="utf-8"), flags=re.MULTILINE)[1:]
    if not sections:
        raise SystemExit(f"error: {CHANGELOG.name} has no '# vX.Y.Z' heading")
    heading, notes = sections[0].strip(), sections[1].strip()
    if heading != f"v{version}":
        raise SystemExit(f"error: {CHANGELOG.name} starts with '# {heading}', not '# v{version}': "
                         "write this release's notes at the top")
    if not notes:
        raise SystemExit(f"error: the notes under '# v{version}' in {CHANGELOG.name} are empty")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(notes + "\n", encoding="utf-8")
    print(f"Wrote {out}: the notes of v{version}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], Path(sys.argv[2]))
