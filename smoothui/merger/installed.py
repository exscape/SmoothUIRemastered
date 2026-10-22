"""Which Smooth UI build is installed in Mods\\: its version and frame rate, and so the release manifest
whose files and settings the merger uses.

The build record (BUILD_RECORD, next to content\\) names the build; without one, the build is found by its
files. Either way, the sizes and CRC32s in the mod's bundle tables of contents must match the manifest's
output_size and output_crc32 for every file, so no file is read. identify() works on that data alone;
installed_build() reads it from the mod folder.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..common import SmoothUIError, profile_key
from ..manifests import (
    BUILD_RECORD,
    MANIFEST_FORMAT,
    is_dev_version,
    load_manifest,
    manifest_format,
    release_manifest_paths,
    version_key,
)
from ..witcher3.mod_bundles import read_bundle_tocs
from .load_order import Mod, is_mod_folder

log = logging.getLogger(__name__)

# Smooth UI's own mod folder, and the name it may be renamed to (to load after other mods); compared
# case-insensitively
SMOOTHUI_FOLDERS = ("modSmoothUIRemastered", "modzzz_SmoothUIRemastered")

class Status(Enum):
    INSTALLED = "installed"         # a release the merger can patch for: version, fps and manifest are set
    NOT_INSTALLED = "not installed"
    DISABLED = "disabled"
    TWO_FOLDERS = "two folders"
    TOO_OLD = "too old"             # from before the 60 and 120 fps builds (no fps in its manifest)
    UNKNOWN = "unknown"             # modified, mixed, or newer than the merger


@dataclass(frozen=True, slots=True)
class Installed:
    status: Status
    message: str                    # for the user: what was found, and what to do unless usable
    folder: str | None = None       # the mod's folder in Mods\
    version: str | None = None      # the build's version, e.g. v0.3.2-120fps
    fps: int | None = None
    manifest: dict | None = None
    sizes_only: bool = False        # matched by sizes only: the manifest has no CRC32s
    newer_release: str | None = None    # a newer Smooth UI release the merger has a manifest for

    @property
    def usable(self) -> bool:
        return self.status == Status.INSTALLED


def is_smoothui_folder(name: str) -> bool:
    """Whether a mod folder is one of Smooth UI's own"""
    return name.lower() in {folder.lower() for folder in SMOOTHUI_FOLDERS}


def smoothui_mod(folders, mods: list[Mod]) -> Mod | Installed:
    """The enabled Smooth UI mod among mods (see load_order.make_mods; folders: every name in Mods\\), or why
    there is none (an Installed that isn't usable)"""
    enabled = [mod for mod in mods if is_smoothui_folder(mod.folder) and mod.enabled]
    if len(enabled) > 1:
        names = " and ".join(mod.folder for mod in enabled)
        return Installed(Status.TWO_FOLDERS, f"two Smooth UI folders are installed ({names}): delete the old one")
    if enabled:
        return enabled[0]
    # Disabled in mods.settings, or renamed to something the game doesn't load (e.g. ~modSmoothUIRemastered)
    disabled = [mod.folder for mod in mods if is_smoothui_folder(mod.folder)]
    disabled += [folder for folder in folders if not is_mod_folder(folder)
                 and any(folder.lower().endswith(name.lower()) for name in SMOOTHUI_FOLDERS)]
    if disabled:
        return Installed(Status.DISABLED, f"Smooth UI Remastered is disabled ({disabled[0]}): enable it first",
                         folder=disabled[0])
    return Installed(Status.NOT_INSTALLED, "Smooth UI Remastered is not installed: install it first")


def identify(folder: str, files: dict[str, tuple[int, int]], record_text: str | None,
             manifests: dict[str, dict]) -> Installed:
    """Which build a Smooth UI mod folder holds.

    files: {profile key: (size, crc32)} of every file in its bundles; record_text: its BUILD_RECORD, or None;
    manifests: {version: manifest} of the release manifests the merger has (see
    release_manifest_paths), dev versions included. Those of a newer format than the merger can read
    (see MANIFEST_FORMAT) only tell that the merger needs an update."""
    too_new = {version for version, manifest in manifests.items() if manifest_format(manifest) > MANIFEST_FORMAT}
    manifests = {version: manifest for version, manifest in manifests.items() if version not in too_new}
    record_version = _record_version(record_text)
    if record_version in too_new:
        return Installed(Status.UNKNOWN, f"Smooth UI Remastered {record_version} is installed, which needs a newer version "
                         "of the merger: update the merger", folder=folder, version=record_version)
    if record_version is not None:
        manifest = manifests.get(record_version)
        if manifest is None:
            return Installed(Status.UNKNOWN, f"Smooth UI Remastered {record_version} is installed, which this version of "
                             "the merger doesn't know: update the merger", folder=folder, version=record_version)
        sizes_only = _matches(files, manifest)
        if sizes_only is None:
            return Installed(Status.UNKNOWN, f"Smooth UI Remastered {record_version} is installed, but its files differ "
                             "from that release's: some are modified, or from another version. Reinstall Smooth UI "
                             "Remastered", folder=folder, version=record_version)
        return _known(folder, record_version, manifest, sizes_only, manifests)

    # Releases first. Dev builds only in the repo (the frozen merger ships no dev manifests), for a dev build
    # installed without a record; a dev manifest is rewritten by every build of its version.
    known = [version for version in manifests if version_key(version) is not None]
    matches = {}
    for dev in (False, True):
        for version in known:
            if is_dev_version(version) == dev and (sizes_only := _matches(files, manifests[version])) is not None:
                matches[version] = sizes_only
        if matches:
            break
    if not matches:
        return Installed(Status.UNKNOWN, "the installed Smooth UI Remastered matches none of the releases this version of "
                         "the merger knows: its files are modified or mixed, or it is newer than the merger. "
                         "Update the merger, or reinstall Smooth UI Remastered", folder=folder)
    # Only possible for a build whose record was deleted: the releases' files all differ
    version = max(matches, key=version_key)
    return _known(folder, version, manifests[version], matches[version], manifests)


def _record_version(record_text):
    """The build version a BUILD_RECORD names, or None (no record, or one that can't be read: the build
    is then found by its files)"""
    if record_text is None:
        return None
    try:
        version = json.loads(record_text)["created_mod_version"]
    except (ValueError, TypeError, KeyError):
        log.warning(f"can't read {BUILD_RECORD}; finding the installed build by its files")
        return None
    return version if isinstance(version, str) else None


def _matches(files, manifest):
    """None unless files are exactly the manifest's output files; else whether only their sizes could be
    compared (a manifest from before output_crc32 was recorded)"""
    records = {profile_key(key): record for key, record in manifest["files"].items()}
    if set(records) != set(files):
        return None
    sizes_only = not all("output_crc32" in record for record in records.values())
    for key, record in records.items():
        size, crc = files[key]
        if size != record["output_size"] or (not sizes_only and crc != record["output_crc32"]):
            return None
    return sizes_only


def _known(folder, version, manifest, sizes_only, manifests):
    if "fps" not in manifest:
        return Installed(Status.TOO_OLD, f"Smooth UI Remastered {version} is installed, which is from before the 60 and "
                         "120 fps builds: update Smooth UI Remastered", folder=folder, version=version)
    releases = [v for v in manifests if version_key(v) is not None and not is_dev_version(v)]
    newest = max(releases, key=version_key, default=None)
    newer = newest.split("-")[0] if newest and version_key(newest) > version_key(version) else None
    message = f"Smooth UI Remastered {version} is installed"
    if sizes_only:
        message += " (identified by file sizes only: its manifest has no CRC32s)"
    if newer:
        message += f"; a newer Smooth UI Remastered is available ({newer})"
    return Installed(Status.INSTALLED, message, folder=folder, version=version, fps=manifest["fps"], manifest=manifest,
                     sizes_only=sizes_only, newer_release=newer)


def installed_build(mods_dir: Path | None, folders, mods: list[Mod], manifests: dict[str, dict] | None = None) -> Installed:
    """The Smooth UI build in mods_dir (the game's Mods\\ folder; see smoothui_mod for folders and mods).
    manifests are as for identify (default: the release manifests the merger has)."""
    mod = smoothui_mod(folders, mods)
    if isinstance(mod, Installed) and mod.status == Status.TWO_FOLDERS:
        return _two_folders([installed_build(mods_dir, folders, [other], manifests) for other in mods
                             if is_smoothui_folder(other.folder) and other.enabled])
    if isinstance(mod, Installed):
        return mod
    folder = Path(mods_dir) / mod.folder
    try:
        files = {key: (entry.size, entry.crc)
                 for key, (_, entry) in read_bundle_tocs(folder / "content").items()}
    except SmoothUIError as e:
        return Installed(Status.UNKNOWN, f"can't read the installed Smooth UI Remastered: {e}", folder=mod.folder)
    try:
        record_text = (folder / BUILD_RECORD).read_text(encoding="utf-8")
    except FileNotFoundError:
        record_text = None
    except (OSError, UnicodeDecodeError) as e:
        log.warning(f"can't read {folder / BUILD_RECORD}: {e}")
        record_text = None
    if manifests is None:
        manifests = {version: manifest for version, path in release_manifest_paths().items()
                     if (manifest := load_manifest(path)) is not None}
    return identify(mod.folder, files, record_text, manifests)


def _two_folders(builds: list[Installed]) -> Installed:
    """For two (or more) enabled Smooth UI folders: their builds, and the older one to delete if known"""
    unknown = "a build the merger doesn't know"
    names = " and ".join(f"{build.folder} ({build.version or unknown})" for build in builds)
    keys = [version_key(build.version or "") for build in builds]
    known = None not in keys and len(set(keys)) == len(keys)
    advice = f"delete {builds[keys.index(min(keys))].folder}, the older one" if known else "delete one of them"
    return Installed(Status.TWO_FOLDERS, f"two Smooth UI folders are installed, and the game loads both: {names}: {advice}")
