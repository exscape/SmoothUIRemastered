"""Whether the installed package is up to date: for each file the installed Smooth UI build patches, what the
package holds, compared with what a build would put in it now (for merge.py --scan, and a GUI's file list).

Read-only, from a scan, the package's manifest and its bundle's table of contents; nothing is patched. So
it tells whether the package was built from the copies the game would use now, not whether a build
would patch them cleanly.

A package that would hold no files is removed, and its manifest kept in a record in the merger's data
folder instead (see package.py), which counts while there is no package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..common import SmoothUIError
from ..files import read_file
from ..paths import DATA_DIR
from ..witcher3.mod_bundles import list_bundles, read_bundle_tocs
from . import merger_version
from .installed import is_smoothui_folder
from .package import PACKAGE_MANIFEST, PACKAGE_MANIFEST_FORMAT, record_path
from .policy import FileStatus
from .scan import COMPAT_PATCH, PACKAGE_FOLDER, Scan, is_package_folder


class State(Enum):
    NOTHING_TO_DO = "nothing to do"     # no other mod ships it, and the package doesn't hold it
    UNDETERMINED = "undetermined"       # the load order doesn't decide which copy wins (see the scan's problems)
    UP_TO_DATE = "up to date"           # the package holds the winning copy, patched, and the game loads it
    NOT_HELD = "not held"               # the last build didn't put it in the package (its status says why), and
                                        # the winning copy is still the one it decided on
    NOT_BUILT = "not built"             # another mod ships it, and the package has no record of it
    CHANGED = "changed"                 # the package was built from another copy (the mod changed, or another
                                        # mod wins now), for another Smooth UI build, or by another version
                                        # of the merger
    NO_LONGER_NEEDED = "no longer needed"   # the package holds it, but no other mod ships it now
    DAMAGED = "damaged"                 # the package's copy isn't the one built, or the package is incomplete
    OVERRIDDEN = "overridden"           # the package holds it, but the game loads another mod's copy, which
                                        # comes before the package in the load order

    @property
    def needs_build(self) -> bool:
        """Whether building the package again would change it. (OVERRIDDEN is about the load order, which a
        build only fixes if the package's own mods.settings entry is the cause.)"""
        return self in (State.NOT_BUILT, State.CHANGED, State.NO_LONGER_NEEDED, State.DAMAGED)


@dataclass(frozen=True, slots=True)
class FileState:
    path: str                   # profile key
    state: State
    message: str                # for the user
    winner: str | None = None   # the mod whose copy a build would patch (see scan.FileResult.winner)


# What the last build did with a file it didn't put in the package (see policy.FileStatus)
NOT_HELD_TEXT = {
    FileStatus.UNCHANGED.value: "needs no patching (already at the target frame rate)",
    FileStatus.LEFT_OUT.value: "left out, as Smooth UI can't patch it entirely",
    FileStatus.FAILED.value: "left out, as it can't be patched",
    FileStatus.MOD_FAILED.value: "left out, as another file of the same mod can't be patched",
}


def package_state(result: Scan, data_dir: Path | None = DATA_DIR) -> list[FileState]:
    """A FileState for each file in the scan, by path (none if the installed Smooth UI can't be used).
    data_dir: the merger's data folder, with the records of packages that hold no files (None: none)."""
    if not result.files:
        return []
    folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), PACKAGE_FOLDER)
    package_dir = result.mods_dir / folder
    manifest, manifest_problem = _read_manifest(package_dir / PACKAGE_MANIFEST)
    toc, toc_problem = _read_toc(package_dir, set(result.files))
    if manifest is None and manifest_problem is None and not toc and toc_problem is None and data_dir is not None:
        manifest, manifest_problem = _read_manifest(record_path(data_dir, result.mods_dir))
    installed = result.installed
    built_for = (manifest or {}).get("smoothui", {})
    built_by = (manifest or {}).get("merger_version")
    if built_for.get("version") != installed.version or built_for.get("release_created_at") != installed.manifest.get("created_at"):
        rebuilt = f"for Smooth UI {built_for.get('version')}, and {installed.version} is installed"
    elif built_by != merger_version():
        rebuilt = f"by version {built_by} of the merger, and this is {merger_version()}"
    else:
        rebuilt = None
    held = (manifest or {}).get("files", {})
    not_held = (manifest or {}).get("not_held", {})

    states = []
    for key in sorted(result.files):
        file = result.files[key]
        winner = file.winner
        origin = winner.origin if winner else None

        def state(kind, message, key=key, origin=origin):
            states.append(FileState(key, kind, message, origin))

        if file.undetermined:
            state(State.UNDETERMINED, "which mod's copy the game loads isn't decided by the load order (see the problems)")
        elif toc_problem:
            state(State.DAMAGED, f"the package can't be read ({toc_problem}): build it again")
        elif key in toc:
            record = held.get(key)
            entry = toc[key][1]
            if record is None:
                why = manifest_problem or "its manifest doesn't record this file"
                state(State.DAMAGED, f"the package holds a copy, but {why}: build it again")
            elif winner is None:
                state(State.NO_LONGER_NEEDED, f"the package holds {record['mod']}'s copy, but no other mod ships it "
                      "now: build again to remove it")
            elif (entry.size, entry.crc) != (record.get("output_size"), record.get("output_crc32")):
                state(State.DAMAGED, "the package's copy isn't the one it built: build it again")
            elif rebuilt:
                state(State.CHANGED, f"the package was built {rebuilt}: build it again")
            elif not _same_copy(record, winner):
                state(State.CHANGED, _changed(record, winner) + ": build it again")
            elif not (file.loaded and is_package_folder(file.loaded.folder)):
                state(State.OVERRIDDEN, f"the package holds it, but the game loads {file.loaded.folder}'s copy: that "
                      "mod comes before the package in the load order. Build again (it gives the package Priority 0), "
                      "or give that mod a higher number than 0 as its priority")
            else:
                notes = ["forced"] if record.get("forced") else []
                if record.get("fixes_not_applied"):
                    notes.append(f"without {', '.join(record['fixes_not_applied'])}")
                state(State.UP_TO_DATE, f"patched from {origin}'s copy" + (f" ({'; '.join(notes)})" if notes else ""))
        elif key in held:
            state(State.DAMAGED, "the package's manifest records it, but its bundle doesn't hold it: build it again")
        elif winner is None:
            state(State.NOTHING_TO_DO, f"no other mod ships it: the game uses {_copy_name(file.loaded.folder)}"
                  if file.loaded else "no mod ships it")
        elif key in not_held:
            record = not_held[key]
            if rebuilt:
                state(State.CHANGED, f"the last build was made {rebuilt}: build again")
            elif not _same_copy(record, winner):
                state(State.CHANGED, f"{_changed(record, winner)}, since the last build, which left it out: build again")
            else:
                state(State.NOT_HELD, f"{origin}'s copy: {NOT_HELD_TEXT.get(record['status'], record['status'])} "
                      "at the last build")
        elif manifest is None and manifest_problem is None:
            state(State.NOT_BUILT, f"{origin}'s copy isn't patched: there is no package. Build it")
        else:
            state(State.NOT_BUILT, f"{origin}'s copy isn't in the package (installed since it was built?): build it again")
    return states


def _read_manifest(path):
    """(the package manifest at path, or None; why it can't be used, or None if there is none)"""
    try:
        data = read_file(path)
    except SmoothUIError as e:
        return None, str(e)
    if data is None:
        return None, None
    try:
        manifest = json.loads(data)
    except ValueError:
        return None, "its manifest can't be read"
    if not isinstance(manifest, dict) or manifest.get("format") != PACKAGE_MANIFEST_FORMAT:
        return None, "its manifest is from another version of the merger"
    return manifest, None


def _read_toc(package_dir, keys):
    """({profile key: (bundle, TocEntry)} for keys in the package's bundles; why they can't be read, or None)"""
    content = package_dir / "content"
    try:
        list_bundles(content)
    except (FileNotFoundError, NotADirectoryError):
        return {}, None
    except OSError as e:
        return {}, str(e)
    try:
        return read_bundle_tocs(content, keys), None
    except SmoothUIError as e:
        return {}, str(e)


def _same_copy(record, winner) -> bool:
    """Whether a manifest record (see package.package_manifest) is about the winner's copy"""
    return (str(record.get("mod", "")).lower() == winner.origin.lower()
            and str(record.get("bundle", "")).lower() == winner.file.name.lower()
            and record.get("source_size") == winner.size and record.get("source_crc32") == winner.toc_entry.crc)


def _changed(record, winner):
    if str(record.get("mod", "")).lower() != winner.origin.lower():
        return f"it was built from {record.get('mod')}'s copy, and {winner.origin}'s comes first now"
    return f"{winner.origin}'s copy changed"


def _copy_name(folder):
    if is_smoothui_folder(folder):
        return "Smooth UI's own copy"
    if COMPAT_PATCH.fullmatch(folder):
        return f"the copy of {folder} (a Smooth UI compat patch)"
    return f"{folder}'s copy"
