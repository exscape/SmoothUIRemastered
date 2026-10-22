"""Where the code finds its files.

Run from the repo (the command-line tools), everything is relative to the repo root. Frozen (PyInstaller,
see smoothui-merger.spec), the tool is one folder: the exes, with the files we ship next to them (manifests\\,
licenses\\), the user's data in data\\, and PyInstaller's _internal\\. Files that belong to a
module (e.g. a fix's .abc) are found relative to that module instead, which works either way.
"""

import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

# The folder holding the exes when frozen, else the repo root. Never the current directory: mod managers
# and Steam start the tool from anywhere.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]

# For files only the command-line tools use (config/, mod_files/, strings/, their output). A frozen tool
# has no repo, so these don't exist there.
REPO_ROOT = APP_DIR

# Release manifests: shipped with the tool; written by build-main.py --make-release
MANIFEST_DIR = APP_DIR / "manifests"

# The user's data: settings, logs, build cache. Release zips never contain it, so unpacking a new version
# over the old one keeps it.
DATA_DIR = APP_DIR / "data"

# Where release manifests are read from. A frozen tool also reads those downloaded since it was released.
MANIFEST_DIRS = (MANIFEST_DIR, DATA_DIR / "manifests") if FROZEN else (MANIFEST_DIR,)
