"""The merger: patches the copies of the files Smooth UI patches that other installed mods ship, into one package mod."""

import re
from functools import cache
from pathlib import Path

from ..common import fail
from ..paths import FROZEN, REPO_ROOT

# Smooth UI Remastered's page
SMOOTHUI_URL = "https://www.nexusmods.com/witcher3/mods/13526"
# Its files: where a newer release is downloaded
FILES_URL = f"{SMOOTHUI_URL}?tab=files"
# Smooth UI Tools' page, and where users report a tools bug (e.g. a file the merger can't patch)
TOOLS_URL = "https://www.nexusmods.com/witcher3/mods/15202"
REPORT_URL = f"{TOOLS_URL}?tab=bugs"
# README_tools.md, rendered: how to set the tools up, per mod manager
TOOLS_README_URL = "https://github.com/exscape/SmoothUIRemastered/blob/main/README_tools.md"
# The release list of the repo the tools come from (see updates.py)
RELEASES_URL = "https://api.github.com/repos/exscape/SmoothUIRemastered/releases"
# For tests only: another release list (a local test server, or a test repo's), e.g.
# http://127.0.0.1:8765/releases. Asset links come from the list, so nothing else changes.
UPDATE_URL_ENV = "SMOOTHUI_UPDATE_URL"
# For tests only, and only with UPDATE_URL_ENV: a GitHub token that can read a private test repo. It is sent
# to GitHub's API only, and assets are then downloaded through the API (a private repo's download links
# need a browser login).
UPDATE_TOKEN_ENV = "SMOOTHUI_UPDATE_TOKEN"

# The merger's version is [versions] tools_version in config/config.toml. A frozen tool has no config:
# the build (build-tools.py) writes the version into this file, at this module's path.
VERSION_FILE = Path(__file__).with_name("tools_version.txt")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


@cache
def tools_version() -> str:
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
                version = tomllib.load(f).get("versions", {}).get("tools_version")
        except (OSError, tomllib.TOMLDecodeError) as e:
            fail(f"can't read the merger's version from {path}: {e}")
        if not isinstance(version, str):
            fail(f"no tools_version in [versions] in {path}")
    if not VERSION.fullmatch(version):
        fail(f"the merger's version must be X.Y.Z, not '{version}'")
    return version
