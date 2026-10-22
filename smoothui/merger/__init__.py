"""The merger: patches the copies of the files Smooth UI patches that other installed mods ship, into one package mod."""

import re
from functools import cache
from pathlib import Path

from ..common import fail
from ..paths import FROZEN, REPO_ROOT

# Where users report a file the merger can't patch
REPORT_URL = "https://www.nexusmods.com/witcher3/mods/13526?tab=bugs"

# The merger's version is [versions] merger_version in config/config.toml. A frozen tool has no config:
# smoothui-merger.spec writes the version into this file (in _internal\, at this module's path).
VERSION_FILE = Path(__file__).with_name("merger_version.txt")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


@cache
def merger_version() -> str:
    """The merger's version, X.Y.Z"""
    if FROZEN:
        try:
            version = VERSION_FILE.read_text(encoding="utf-8").strip()
        except OSError as e:
            fail(f"can't read the merger's version: {e}")
    else:
        # Only the repo has a config. Read here, not with build.shared's load_config: that imports the
        # patcher, which the launcher must not (and PyInstaller would then bundle it into the launcher).
        # tomllib is imported here too, as a frozen tool never needs it.
        import tomllib
        path = REPO_ROOT / "config" / "config.toml"
        try:
            with open(path, "rb") as f:
                version = tomllib.load(f).get("versions", {}).get("merger_version")
        except (OSError, tomllib.TOMLDecodeError) as e:
            fail(f"can't read the merger's version from {path}: {e}")
        if not isinstance(version, str):
            fail(f"no merger_version in [versions] in {path}")
    if not VERSION.fullmatch(version):
        fail(f"the merger's version must be X.Y.Z, not '{version}'")
    return version
