"""Smooth UI's build versions (e.g. v0.3.2-120fps) and its release manifests (manifests/<version>.json),
as the build tools write them and the merger reads them. Light: the merger's launch check imports it.
"""

import json
import re
import subprocess
from pathlib import Path

from .common import normalize_version
from .paths import FROZEN, MANIFEST_DIR, MANIFEST_DIRS, REPO_ROOT

# A build's version: the base version (created_mod_version in config.toml), then the frame rate
BUILD_VERSION = re.compile(r"(v.+?)-(\d+)fps")
# A base version: numbers, then an optional suffix, e.g. v0.3, v0.3.1, v0.3.2dev, v0.3beta
VERSION = re.compile(r"v(\d+(?:\.\d+)*)([a-z][a-z0-9]*)?")
# Versions with the same numbers, from least to most recent; other suffixes come before these
SUFFIXES = ("beta", "dev", "")
# The frame rate of manifests from before the 60 and 120 fps builds
OLD_MANIFEST_FPS = 120
# The frame rates of a release's builds, in the order they are built and reported
RELEASE_FPS = (120, 60)


def build_version(base_version, fps):
    """The version of a build at fps, e.g. v0.3-60fps"""
    return f"{normalize_version(base_version)}-{fps}fps"

def split_build_version(version):
    """(base version, fps) for a build's version (see build_version); fps is None for versions
    without a frame rate (manifests from before the 60 and 120 fps builds)"""
    version = normalize_version(version)
    match = BUILD_VERSION.fullmatch(version)
    return (match[1], int(match[2])) if match else (version, None)

def version_key(version):
    """Sort key for versions like v0.3.1, v0.3beta or v0.3-60fps (the frame rate is ignored; see
    build_fps), and other mods' versions like v1.55; None if it isn't one. The numbers compare as
    integers (v0.3.10 comes after v0.3.9), then the suffix (see SUFFIXES)."""
    match = VERSION.fullmatch(split_build_version(version)[0])
    if not match:
        return None
    suffix = match[2] or ""
    return tuple(int(number) for number in match[1].split(".")), SUFFIXES.index(suffix) if suffix in SUFFIXES else -1

def is_dev_version(version):
    key = version_key(version)
    return key is not None and key[1] == SUFFIXES.index("dev")

def build_fps(version):
    """The frame rate of the build a version names"""
    return split_build_version(version)[1] or OLD_MANIFEST_FPS

def release_manifest_paths():
    """{version: path} of every release manifest (<version>.json) in MANIFEST_DIRS. A version found in
    several of them (a downloaded copy of one the tool also ships) uses the copy built last."""
    def created_at(path):
        return (load_manifest(path) or {}).get("created_at") or ""
    found = {}
    for directory in MANIFEST_DIRS:
        for path in directory.glob("*.json"):
            if path.stem not in found or created_at(path) > created_at(found[path.stem]):
                found[path.stem] = path
    return found

def release_manifests(fps):
    """(version, path) of each release manifest of a build at fps, oldest first"""
    found = [(version, path) for version, path in release_manifest_paths().items()
             if version_key(version) is not None and build_fps(version) == fps]
    return sorted(found, key=lambda item: version_key(item[0]))

def latest_manifests():
    """{fps: path} of the most recent release manifest at each release frame rate, dev builds included"""
    return {fps: manifests[-1][1] for fps in RELEASE_FPS if (manifests := release_manifests(fps))}

def manifest_path(version):
    """Where build-main.py writes a mod release's manifest: manifests/<version>.json"""
    return MANIFEST_DIR / f"{normalize_version(version)}.json"

def load_manifest(version_or_path):
    """A manifest, given a release's version (see release_manifest_paths) or a path to it; None if it
    doesn't exist"""
    if str(version_or_path).endswith(".json"):
        path = Path(version_or_path)
    else:
        path = release_manifest_paths().get(normalize_version(version_or_path))
    if path is None or not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

# The format of the release manifests this code writes and can read (their "manifest_format"; one without
# it is format 1). Increased when a change means that older code can't use a manifest, e.g. a new kind of
# setting: a merger that gets a manifest of a newer format (shipped with a newer Smooth UI, or downloaded)
# must be updated before it can patch for that release.
MANIFEST_FORMAT = 1

def manifest_format(manifest: dict) -> int:
    return manifest.get("manifest_format", 1)

# Which build a Smooth UI mod folder holds (its version and frame rate), next to content\ (the game only
# reads content\). The merger identifies the installed build by it, checked against that build's manifest.
BUILD_RECORD = "smoothui_build.json"

def patcher_commit():
    """(commit, uncommitted) for the code doing the build: the git commit hash of this checkout, and
    whether tracked files have uncommitted changes. (None, None) if git can't tell (e.g. no checkout)."""
    if FROZEN:
        return None, None   # no checkout, and the tool's folder may be inside someone else's
    def git(*args):
        # --no-optional-locks: don't let `git status` refresh the index, so nothing is written
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    try:
        return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.CalledProcessError):
        return None, None
