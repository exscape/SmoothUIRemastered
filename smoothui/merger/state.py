"""Whether the installed package is up to date: for each file the installed Smooth UI build patches, what the
package holds, compared with what a build would put in it now (package_state), and from that, whether the
merger should open before the game starts (check: the launcher, merge.py --scan and the merger's window
all decide by it).

Read-only, from a scan, the package's manifest and its bundle's table of contents; nothing is patched. So
it tells whether the package was built from the copies the game would use now, not whether a build
would patch them cleanly. Light: the launcher imports it, so it must not import the patcher.

A package that would hold no files is removed, and its manifest kept in a record in the merger's data
folder instead (see package.py), which counts while there is no package.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path

from ..common import SmoothUIError
from ..files import read_file
from ..paths import DATA_DIR
from ..witcher3.mod_bundles import list_bundles, read_bundle_tocs
from ..witcher3.mods_settings import ModSettings
from . import merger_version
from .installed import is_smoothui_folder
from .package import PACKAGE_MANIFEST, PACKAGE_MANIFEST_FORMAT, record_path
from .scan import COMPAT_PATCH, PACKAGE_FOLDER, Scan, is_package_folder, scan


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
        """Whether building the package again always changes it. (OVERRIDDEN is about the load order, which a
        build only fixes if the package's own mods.settings entry is the cause: see FileState.needs_build.)"""
        return self in (State.NOT_BUILT, State.CHANGED, State.NO_LONGER_NEEDED, State.DAMAGED)


class Severity(IntEnum):
    """What playing without building the package again costs"""
    LOW = 1         # nothing: only the merger's version changed since the build
    MEDIUM = 2      # a mod's copy that the game loads isn't patched: it runs at its original frame rate
    HIGH = 3        # the package overrides a file with a copy that doesn't fit what is installed now (a mod
                    # updated or removed, another mod's copy wins, another Smooth UI build, a damaged
                    # package): that screen may look wrong, or that mod may not work at all


@dataclass(frozen=True, slots=True)
class FileState:
    path: str                   # profile key
    state: State
    message: str                # for the user
    winner: str | None = None   # the mod whose copy a build would patch (see scan.FileResult.winner)
    severity: Severity | None = None    # set for the files that open the merger at launch (see Check)
    needs_build: bool = False   # whether building the package again would change it (see State.needs_build)


@dataclass(frozen=True, slots=True)
class Check:
    """What check found"""
    scan: Scan
    states: list[FileState]         # see package_state
    package_without_smoothui: bool  # a package is installed, but the installed Smooth UI can't be used
                                    # (scan.installed.message says why): the package overrides files with
                                    # copies patched for another Smooth UI build, or for none

    @property
    def stale(self) -> bool:
        """Whether the launcher opens the merger before the game starts. The scan's problems never do by
        themselves: a build can't fix them (the merger lists them when it opens anyway); scan.conflicts do."""
        return self.severity is not None

    @property
    def severity(self) -> Severity | None:
        if self.package_without_smoothui or self.scan.conflicts:
            return Severity.HIGH
        return max((state.severity for state in self.states if state.severity is not None), default=None)

    @property
    def needs_build(self) -> list[FileState]:
        """The files that building the package again would change"""
        return [state for state in self.states if state.needs_build]


def check(game_path: Path, mods_settings: Path, *, data_dir: Path | None = DATA_DIR, **scan_options) -> Check:
    """Scan (scan_options go to scan.scan, for tests), and compare with the package (see package_state;
    data_dir as there). Fails (SmoothUIError) only if the scan does."""
    result = scan(game_path, mods_settings, **scan_options)
    package_without_smoothui = not result.installed.usable and _has_package(result)
    return Check(result, package_state(result, data_dir), package_without_smoothui)


def _has_package(result: Scan) -> bool:
    """Whether the game loads a package (an enabled package folder with a bundle)"""
    folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), None)
    if folder is None:
        return False
    try:
        return bool(list_bundles(result.mods_dir / folder / "content"))
    except OSError:
        return False    # no content folder: nothing for the game to load


# What the last build did with a file it didn't put in the package, by the status the manifest records
# (policy.FileStatus's values; policy.py isn't imported here, as the launch check doesn't need the patcher)
NOT_HELD_TEXT = {
    "unchanged": "needs no patching (already at the target frame rate)",
    "left out": "left out, as Smooth UI can't patch it entirely",
    "failed": "left out, as it can't be patched",
    "mod failed": "left out, as another file of the same mod can't be patched",
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
    # Only the merger's version changed: nothing the game shows is wrong (rebuilt_severity LOW)
    if built_for.get("version") != installed.version or built_for.get("release_created_at") != installed.manifest.get("created_at"):
        rebuilt = f"for Smooth UI {built_for.get('version')}, and {installed.version} is installed"
        merger_only = False
    elif built_by != merger_version():
        rebuilt = f"by version {built_by} of the merger, and this is {merger_version()}"
        merger_only = True
    else:
        rebuilt = None
    # Whether the package's own mods.settings entry is as a build writes it (see mods_settings.write_entry)
    own_entry = any(is_package_folder(mod.folder) and mod.settings == ModSettings(True, 0) for mod in result.order)
    held = (manifest or {}).get("files", {})
    not_held = (manifest or {}).get("not_held", {})

    states = []
    for key in sorted(result.files):
        file = result.files[key]
        winner = file.winner
        origin = winner.origin if winner else None

        def state(kind, message, severity=None, needs_build=None, key=key, origin=origin):
            needs_build = kind.needs_build if needs_build is None else needs_build
            states.append(FileState(key, kind, message, origin, severity, needs_build))

        if file.undetermined:
            state(State.UNDETERMINED, "which mod's copy the game loads isn't decided by the load order (see the problems)")
        elif toc_problem and (key in held or (manifest is None and winner is not None)):
            state(State.DAMAGED, f"the package can't be used ({toc_problem}): build it again", Severity.HIGH)
        elif key in toc:
            record = held.get(key)
            entry = toc[key][1]
            if record is None:
                why = manifest_problem or "its manifest doesn't record this file"
                state(State.DAMAGED, f"the package holds a copy, but {why}: build it again", Severity.HIGH)
            elif winner is None:
                state(State.NO_LONGER_NEEDED, f"the package holds {record['mod']}'s copy, but no other mod ships it "
                      "now: build again to remove it", Severity.HIGH)
            elif (entry.size, entry.crc) != (record.get("output_size"), record.get("output_crc32")):
                state(State.DAMAGED, "the package's copy isn't the one it built: build it again", Severity.HIGH)
            elif rebuilt:
                state(State.CHANGED, f"the package was built {rebuilt}: build it again",
                      Severity.LOW if merger_only else Severity.HIGH)
            elif not _same_copy(record, winner):
                state(State.CHANGED, _changed(record, winner) + ": build it again", Severity.HIGH)
            elif not (file.loaded and is_package_folder(file.loaded.folder)):
                loaded = file.loaded.folder if file.loaded else "another mod"
                if own_entry:
                    state(State.OVERRIDDEN, f"the package holds it, but the game loads {loaded}'s copy: that mod has "
                          "Priority 0 too, and its name comes first. Give it a higher number than 0 as its priority "
                          "(in mods.settings, or in your mod manager), then scan again", Severity.MEDIUM)
                else:
                    state(State.OVERRIDDEN, f"the package holds it, but the game loads {loaded}'s copy, as the "
                          "package's mods.settings entry is missing, disabled, or not at Priority 0: build again "
                          "to write it", Severity.MEDIUM, needs_build=True)
            else:
                notes = ["forced"] if record.get("forced") else []
                if record.get("fixes_not_applied"):
                    notes.append(f"without {', '.join(record['fixes_not_applied'])}")
                state(State.UP_TO_DATE, f"patched from {origin}'s copy" + (f" ({'; '.join(notes)})" if notes else ""))
        elif key in held:
            state(State.DAMAGED, "the package's manifest records it, but its bundle doesn't hold it: build it again",
                  Severity.HIGH)
        elif winner is None:
            state(State.NOTHING_TO_DO, f"no other mod ships it: the game uses {_copy_name(file.loaded.folder)}"
                  if file.loaded else "no mod ships it")
        elif key in not_held:
            # The package doesn't hold it, so the game shows the winning copy, unpatched
            record = not_held[key]
            if rebuilt:
                state(State.CHANGED, f"the last build was made {rebuilt}: build again",
                      Severity.LOW if merger_only else Severity.MEDIUM)
            elif not _same_copy(record, winner):
                state(State.CHANGED, f"{_changed(record, winner)}, since the last build, which left it out: build again",
                      Severity.MEDIUM)
            else:
                state(State.NOT_HELD, f"{origin}'s copy: {NOT_HELD_TEXT.get(record['status'], record['status'])} "
                      "at the last build")
        elif manifest is None and manifest_problem is None:
            state(State.NOT_BUILT, f"{origin}'s copy isn't patched: there is no package. Build it", Severity.MEDIUM)
        else:
            state(State.NOT_BUILT, f"{origin}'s copy isn't in the package (installed since it was built?): build it again",
                  Severity.MEDIUM)
    return states


def carried_force(result: Scan) -> tuple[list[str], list[str]]:
    """The files the package holds because the last build forced them (see policy.decide), to force again
    in the next build: those whose copy is still the one it forced. And a note for each other one, which
    isn't forced, so that the user decides again."""
    folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), PACKAGE_FOLDER)
    manifest, _ = _read_manifest(result.mods_dir / folder / PACKAGE_MANIFEST)
    force, notes = [], []
    for key, record in sorted((manifest or {}).get("files", {}).items()):
        if not record.get("forced"):
            continue
        file = result.files.get(key)
        if file is not None and file.winner is not None and _same_copy(record, file.winner):
            force.append(key)
        else:
            notes.append(f"{key} was forced into the package at the last build, but its copy changed: it isn't "
                         "forced now")
    return force, notes


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
        bundles = list_bundles(content)
        names = {entry.name.lower() for entry in os.scandir(content)}
    except (FileNotFoundError, NotADirectoryError):
        return {}, None
    except OSError as e:
        return {}, str(e)
    if bundles and "metadata.store" not in names:
        return {}, "it has no metadata.store, so the game doesn't load it"
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
