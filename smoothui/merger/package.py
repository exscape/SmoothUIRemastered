"""The package: the merger's own mod folder in Mods\\ (scan.PACKAGE_FOLDER), holding the patched files.

Its files, all written by files.py's rules:
- content\\blob0.bundle and content\\metadata.store: the files the package holds (none, if it holds none)
- PACKAGE_MANIFEST: what it holds and what it was built from. Written last, and deleted first, so a
  package without one is incomplete (an interrupted build).

Nothing else in the folder is touched.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import NamedTuple

from ..build.shared import pack_entries, patcher_commit
from ..files import delete_file, make_dirs, remove_empty_dir, write_file
from . import merger_version
from .installed import Installed
from .policy import FileStatus, PackageFile

log = logging.getLogger(__name__)

PACKAGE_MANIFEST = "smoothui_package.json"
BUNDLE = "blob0.bundle"
STORE = "metadata.store"
# Every file the merger writes in the package, relative to it
OWN_FILES = (f"content/{BUNDLE}", f"content/{STORE}", PACKAGE_MANIFEST)
# The package manifest's format: increased when it changes in a way that older readers can't handle
PACKAGE_MANIFEST_FORMAT = 1


class Written(NamedTuple):
    """What write_package returns"""
    manifest: dict
    unknown_files: list[str]    # files in the package folder that the merger didn't write, relative to it


def write_package(package_dir: Path, installed: Installed, files: list[PackageFile]) -> Written:
    """Write the package into package_dir (created if needed), replacing what it held: the files in files
    that are to be included (see FileStatus.included), and a manifest recording files (see
    package_manifest). installed: the Smooth UI build they were patched for."""
    package_dir = Path(package_dir)
    content = package_dir / "content"
    delete_file(package_dir / PACKAGE_MANIFEST)
    included = sorted((file for file in files if file.status.included), key=lambda file: file.path)
    if included:
        bundle, store = pack_entries((file.entry for file in included), BUNDLE)
        make_dirs(content)
        write_file(content / BUNDLE, bundle)
        write_file(content / STORE, store)
        log.info(f"Wrote {content / BUNDLE} ({len(included)} files, {len(bundle) / 2**20:.1f} MiB) and its {STORE}")
    else:
        # A folder without bundles is one the game loads nothing from
        for name in (BUNDLE, STORE):
            delete_file(content / name)
        remove_empty_dir(content)
        make_dirs(package_dir)
        log.info("The package holds no files")
    manifest = package_manifest(installed, files)
    write_file(package_dir / PACKAGE_MANIFEST, (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return Written(manifest, _unknown_files(package_dir))


def package_manifest(installed: Installed, files: list[PackageFile]) -> dict:
    """What the package holds, and what it was built from: for each file it holds, the copy it was patched
    from (to tell whether that changed without reading it) and the output (to tell whether the package
    is intact); for each other file, why it isn't there."""
    commit, uncommitted = patcher_commit()
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
                "forced": file.status == FileStatus.FORCED,
                "fixes_not_applied": file.fixes_not_applied,
            }
        else:
            not_held[file.path] = {
                "mod": file.mod,
                "status": file.status.value,
                "reasons": [file.error] if file.error else [issue.message for issue in file.issues],
            }
    return {
        "format": PACKAGE_MANIFEST_FORMAT,
        "merger_version": merger_version(),
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
        "files": held,
        "not_held": not_held,
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
