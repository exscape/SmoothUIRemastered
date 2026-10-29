"""The package: the merger's own mod folder in Mods\\ (scan.PACKAGE_FOLDER), holding the patched files.

Its files, all written by files.py's rules:
- content\\blob0.bundle and content\\metadata.store: the files the package holds (none, if it holds none)
- PACKAGE_MANIFEST: what it holds and what it was built from. Written last, and deleted first, so a
  package without one is incomplete (an interrupted build).

Nothing else in the folder is touched. A package that would hold no files isn't written: a mod folder
without bundles may look broken to the game or a mod manager, so the merger's files are deleted, and the
folder too, unless it holds other files. Its manifest then goes to a record in the merger's data folder
(see record_path) instead, so that what the build decided is still known.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..files import delete_file, make_dirs, remove_empty_dir, write_file
from ..manifests import patcher_commit
from ..witcher3.mod_bundles import pack_entries
from . import tools_version
from .installed import Installed

if TYPE_CHECKING:
    # Not imported otherwise: the launch check reads PACKAGE_MANIFEST, and policy.py imports the patcher
    from .policy import PackageFile

log = logging.getLogger(__name__)

PACKAGE_MANIFEST = "smoothui_package.json"
BUNDLE = "blob0.bundle"
STORE = "metadata.store"
# Every file the merger writes in the package, relative to it
OWN_FILES = (f"content/{BUNDLE}", f"content/{STORE}", PACKAGE_MANIFEST)
# The package manifest's format: increased when it changes in a way that older readers can't handle
PACKAGE_MANIFEST_FORMAT = 1
# The folder, in the merger's data folder, of the records of packages that hold no files
RECORDS = "packages"


class Written(NamedTuple):
    """What write_package returns"""
    manifest: dict              # see package_manifest: in the package, or in its record if it holds no files
    unknown_files: list[str]    # files in the package folder that the merger didn't write, relative to it
    removed: bool = False       # it holds no files, and its folder is gone (or never was there)

    @property
    def empty(self) -> bool:
        """Whether it holds no files, so there is no package"""
        return not self.manifest["files"]


def record_path(data_dir: Path, mods_dir: Path) -> Path:
    """Where the record of the package for a Mods\\ folder goes, while it holds no files: one file per Mods\\
    folder, in data_dir's RECORDS"""
    key = os.path.normcase(os.path.abspath(mods_dir)).encode("utf-8")
    return Path(data_dir) / RECORDS / f"{hashlib.sha256(key).hexdigest()[:16]}.json"


def write_package(package_dir: Path, installed: Installed, files: list[PackageFile], record: Path | None = None, *,
                  reviews_sha256: str | None, groups_off=(), screens_off=()) -> Written:
    """Write the package into package_dir (created if needed), replacing what it held: the files in files
    that are to be included (see policy.FileStatus.included), and a manifest recording files (see
    package_manifest). installed: the Smooth UI build they were patched for; reviews_sha256: the reviews of
    mods' code they were patched with (see mod_reviews.py); groups_off and screens_off: the groups and
    files turned off (see scan.Scan.groups_off and screens_off; the latter is a dict). If none is to be included, remove the
    package instead, and write the manifest to record (see above), if given; else delete record."""
    package_dir = Path(package_dir)
    content = package_dir / "content"
    previous = _read_manifest(package_dir / PACKAGE_MANIFEST)
    delete_file(package_dir / PACKAGE_MANIFEST)
    included = sorted((file for file in files if file.status.included), key=lambda file: file.path)
    manifest = package_manifest(installed, files, previous, reviews_sha256, groups_off, screens_off)
    if not included:
        removed, unknown = remove_package(package_dir)
        log.info("Nothing to merge: " + ("removed the merged patches" if removed else "their folder stays, as it holds other files"))
        if record is not None:
            make_dirs(Path(record).parent)
            write_file(record, _manifest_bytes(manifest))
        return Written(manifest, unknown, removed)

    bundle, store = pack_entries((file.entry for file in included), BUNDLE)
    make_dirs(content)
    write_file(content / BUNDLE, bundle)
    write_file(content / STORE, store)
    log.info(f"Wrote {content / BUNDLE} ({len(included)} files, {len(bundle) / 2**20:.1f} MiB) and its {STORE}")
    write_file(package_dir / PACKAGE_MANIFEST, _manifest_bytes(manifest))
    if record is not None:
        delete_file(record)
    return Written(manifest, _unknown_files(package_dir))


def remove_package(package_dir: Path) -> tuple[bool, list[str]]:
    """Delete the merger's own files in package_dir (OWN_FILES, the manifest first), then its folders if
    they are empty; nothing else. mods.settings is left alone: its entry for the package stays, as a later
    build would write it again. Returns whether the folder is gone (also if it never was there), and the
    files left in it that the merger didn't write."""
    package_dir = Path(package_dir)
    delete_file(package_dir / PACKAGE_MANIFEST)
    for name in (BUNDLE, STORE):
        delete_file(package_dir / "content" / name)
    remove_empty_dir(package_dir / "content")
    unknown = _unknown_files(package_dir)
    return not unknown and remove_empty_dir(package_dir), unknown


def _manifest_bytes(manifest):
    return (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _read_manifest(path) -> dict:
    """The package manifest at path, or {} if there is none or it can't be read"""
    try:
        manifest = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def same_copy(record: dict, source) -> bool:
    """Whether a manifest record (see package_manifest) is about a copy (a SourceFile): the same mod, bundle,
    size and CRC32"""
    return (str(record.get("mod", "")).lower() == source.origin.lower()
            and str(record.get("bundle", "")).lower() == source.file.name.lower()
            and record.get("source_size") == source.size and record.get("source_crc32") == source.toc_entry.crc)


def package_manifest(installed: Installed, files: list[PackageFile], previous: dict | None = None,
                     reviews_sha256: str | None = None, groups_off=(), screens_off=()) -> dict:
    """What the package holds, and what it was built from: for each file it holds, the copy it was patched
    from (to tell whether that changed without reading it) and the output (to tell whether the package
    is intact); for each other file, why it isn't there, and the copy that was decided on. A forced file
    also gets why it isn't fully patched, and the date it was first forced: from previous (the manifest
    of the package this one replaces) while its copy is the same, else today."""
    commit, uncommitted = patcher_commit()
    before = (previous or {}).get("files", {})
    today = datetime.date.today().isoformat()
    held, not_held = {}, {}
    for file in files:
        if file.status.included:
            held[file.path] = {
                "mod": file.mod,
                "bundle": file.source.file.name,
                "source_size": file.source.size,
                "source_crc32": file.source.toc_entry.crc,
                "output_size": file.entry.size,
                "output_crc32": file.entry.crc,
                "forced": file.forced,
                "fixes_not_applied": file.fixes_not_applied,
            }
            if file.forced:
                old = before.get(file.path, {})
                kept = old.get("forced_since") if old.get("forced") and same_copy(old, file.source) else None
                held[file.path]["forced_since"] = kept or today
                held[file.path]["reasons"] = [issue.message for issue in file.issues if issue.keeps_out]
                # Why, in a word, for the user (see state.WHY)
                held[file.path]["reason"] = file.reason
        else:
            not_held[file.path] = {
                "mod": file.mod,
                "bundle": file.source.file.name,
                "source_size": file.source.size,
                "source_crc32": file.source.toc_entry.crc,
                "status": file.status.value,
                "reasons": [file.error] if file.error else [issue.message for issue in file.issues],
            }
            if file.status.value == "left out":
                not_held[file.path]["reason"] = file.reason      # see above
            if file.status.value == "known broken":
                # Its other issues don't matter: it is left out anyway
                not_held[file.path]["reasons"] = [issue.message for issue in file.issues if issue.review in file.known_broken]
                # Whether it still is, once the reviews of mods' code change (see state.package_state)
                not_held[file.path]["known_broken"] = file.known_broken
    return {
        "format": PACKAGE_MANIFEST_FORMAT,
        "tools_version": tools_version(),
        "patcher_commit": commit,
        "patcher_uncommitted_changes": uncommitted,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "smoothui": {
            "folder": installed.folder,
            "version": installed.version,
            "fps": installed.fps,
            # A release's manifest is rewritten when it is rebuilt; this tells which build it was
            "release_created_at": installed.manifest.get("created_at"),
        },
        # The reviews of mods' code used (see mod_reviews.py): new ones may let a screen that was left out be merged
        "mod_reviews_sha256": reviews_sha256,
        "files": held,
        "not_held": not_held,
        # The groups turned off (see scan.Scan.groups_off): their files are in neither of the above
        "groups_off": sorted(groups_off),
        # The files the user chose not to merge (see scan.Scan.screens_off): in neither either
        "screens_off": sorted(screens_off),
        # Which of those our rules left out (a dont-merge line, see mod_reviews.py), and why: not the user's choice
        "screens_off_by_rule": {key: choice.reason for key, choice in sorted(dict(screens_off).items()) if choice.reason},
    }


def _unknown_files(package_dir):
    """The files in package_dir that the merger didn't write (relative, with / separators)"""
    found = []
    for folder, _, names in os.walk(package_dir):
        for name in names:
            relative = Path(folder, name).relative_to(package_dir).as_posix()
            if relative.lower() not in {own.lower() for own in OWN_FILES}:
                found.append(relative)
    return sorted(found)
