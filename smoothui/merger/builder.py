"""Building the package: scan (scan.py) -> patch the winning copies (patch_winners) -> choose what goes in
(policy.decide) -> write it (install) -> check that the game loads it (check_package, on a new scan).

build_package runs them all. A GUI can run them one by one instead: show the scan, patch once, then call
decide again whenever the user changes a choice, and install when they are done.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..build.release import (
    ReleasePatch,
    patch_as_release,
    profile_from_manifest,
    with_unlisted_files,
)
from ..build.reviewed_warnings import WarningSummary
from ..build.shared import print_file_result
from ..common import fail, profile_key
from ..files import make_dirs
from ..patcher.patcher_core import RuleIssues
from ..paths import DATA_DIR
from ..requires import OPTIONAL_FIXES
from ..witcher3.bundle import BundleError, read_entry
from ..witcher3.mods_settings import read_mods_settings, write_entry
from .game import MODS_SETTINGS_BACKUP
from .package import Written, record_path, write_package
from .policy import AsIs, PackageFile, decide
from .scan import PACKAGE_FOLDER, Scan, entry_needed, is_package_folder, scan
from .screens import file_name, screen_label
from .status import FileStatus, Level

log = logging.getLogger(__name__)


def patch_winners(result: Scan, *, pool=None, on_file=print_file_result) -> ReleasePatch:
    """Patch the winning copy of each file (see scan.FileResult.winner) with the installed Smooth UI
    build's settings at the scan's frame rate for it (see Scan.fps_of), plus the optional fixes chosen for it
    and the dont-stretch lines for its mod, hiding the warnings reviewed for it (both in the scan's reviews, see
    mod_reviews.py). pool and on_file are as for patch_as_release."""
    installed = result.installed
    if not installed.usable:
        fail(installed.message)
    sources = [file.winner for file in result.files.values() if file.winner]
    if not sources:
        return ReleasePatch([], [], [], WarningSummary({}, 0, [], 0), RuleIssues([], []), {})
    # The files that mods add to a group are patched as the release would have patched them
    manifest = with_unlisted_files(installed.manifest, {source.path: result.group_of[source.path] for source in sources
                                                        if source.path in result.group_of})
    try:
        profile = {profile_key(key): settings for key, settings in profile_from_manifest(manifest).items()}
    except (KeyError, TypeError, ValueError) as e:     # a release that should have raised manifest_format
        fail(f"this version of Smooth UI Tools can't read the manifest of Smooth UI Remastered {installed.version} "
             f"({e!r}): update them")
    settings = {}
    for source in sources:
        optional = tuple(fix for fix in sorted(result.optional_fixes) if source.path in OPTIONAL_FIXES[fix])
        settings[source.path] = replace(profile[source.path], target_fps=result.fps_of(source.path),
                                        fixes=profile[source.path].fixes + optional)
        if result.reviews and (extra := result.reviews.dont_stretch(source.origin, file_name(source.path),
                                                                     result.group_of.get(source.path))):
            settings[source.path] = replace(settings[source.path], exclude=settings[source.path].exclude + extra)
    return patch_as_release(sources, settings, manifest, mod_reviews=result.reviews, on_file=on_file, pool=pool)


def merges_now(result: Scan, keys, *, pool=None) -> list[str]:
    """Which of keys, files the user chose not to merge (see Scan.screens_off), would merge now: their copies
    (Scan.off_copies) patched as a build would, as a trial (nothing is written), without anything that would
    leave them out. Each one alone: the rest of its group is the build's to check, once it is turned on. pool: as
    for patch_winners."""
    keys = [key for key in keys if key in result.off_copies]
    if not keys:
        return []
    trial = replace(result, files={key: replace(result.files[key], winner=result.off_copies[key]) for key in keys},
                    group_of={key: result.group_of[key] for key in keys if key in result.group_of},
                    screens_off={}, off_copies={})
    patch = patch_winners(trial, pool=pool, on_file=lambda *args, **kwargs: None)
    return [file.path for file in decide(trial, patch) if file.status in (FileStatus.PATCHED, FileStatus.UNCHANGED)]


@dataclass(slots=True)
class Installation:
    """What install returns"""
    package_dir: Path
    written: Written
    entry_changed: bool | None      # whether mods.settings changed; None if it was left alone: the package
                                    # went elsewhere, or holds no files


def install(result: Scan, files: list[PackageFile], mods_settings: Path, *, out_dir: Path | None = None,
            data_dir: Path | None = DATA_DIR, as_is=()) -> Installation:
    """Write the package into the game's Mods\\ folder and give it its mods.settings entry (the file at
    mods_settings; its old content goes to MODS_SETTINGS_BACKUP in data_dir first, if it changes). The
    file is only written if another mod (not mod0000_MergedFiles) loads before the package. With
    out_dir, write the package there instead, for a look at it, and leave mods.settings alone. A package
    that holds no files is removed instead, with a record in data_dir (see package.py), and mods.settings
    is left alone: an entry it has stays, as a later build would write it again. data_dir None: no
    backup and no record. The package records the scan's reviews, which the files were patched with."""
    assert result.mods_dir is not None  # a usable scan
    package_dir = Path(out_dir or result.mods_dir) / PACKAGE_FOLDER
    record = record_path(data_dir, result.mods_dir) if data_dir is not None and out_dir is None else None
    written = write_package(package_dir, result.installed, files, record, reviews_sha256=result.reviews.sha256,
                            groups_off=result.groups_off, screens_off=result.screens_off,
                            rates=(result.target_fps, result.hud_fps), as_is=as_is,
                            optional_fixes=result.optional_fixes)
    if out_dir is not None or written.empty:
        return Installation(package_dir, written, None)
    backup = None
    if data_dir is not None:
        make_dirs(data_dir)
        backup = Path(data_dir) / MODS_SETTINGS_BACKUP
    if not entry_needed(result.order, read_mods_settings(mods_settings).get(PACKAGE_FOLDER.lower())):
        return Installation(package_dir, written, False)
    return Installation(package_dir, written, write_entry(mods_settings, PACKAGE_FOLDER, backup=backup))


def as_is_copies(scan: Scan, files: list[PackageFile]) -> list[AsIs]:
    """The mods' copies that aren't merged, but that the game wouldn't load, as Smooth UI's copy comes first:
    the package holds them as they are, as a mod is installed for its changes. So the game gets what it would if
    the mod came first. Not a group's: it is merged whole or not at all, and Smooth UI doesn't ship the HUD."""
    copies = [(file.path, file.source, file.status) for file in files if not file.status.included]
    copies += [(key, copy, FileStatus.BY_RULE if scan.screens_off[key].reason else FileStatus.BY_CHOICE)
               for key, copy in scan.off_copies.items()]
    return [AsIs(path, source, _read_copy(source), status) for path, source, status in copies
            if path not in scan.group_of and scan.files[path].smoothui_first and source.toc_entry is not None]


def _read_copy(source):
    """A mod's copy as a BundleEntry, or None if its bundle is damaged there. Only that: a file the patcher can't
    read may well work in game. Its path is spelled as the package's files are: a mod's bundle may have capitals or
    forward slashes, which the package's metadata.store can't hold."""
    try:
        with open(source.file, "rb") as f:
            entry = read_entry(f, source.toc_entry)
        entry.content()
    except (OSError, BundleError) as e:
        log.warning(f"{screen_label(source.path)}: {source.origin}'s copy can't be read, so it can't be used: {e}")
        return None
    return entry._replace(path=source.path.replace("/", "\\"))


def check_package(files: list[PackageFile], rescan: Scan, as_is=()) -> list[str]:
    """Problems with an installed package, from a scan made after it was installed: each file it holds must
    be loaded from it, and still come from the same copy (else a mod changed while it was built). as_is: the
    copies it holds as they are (see as_is_copies)."""
    if not rescan.installed.usable:
        return [f"the merged patches can't be checked: {rescan.installed.message}"]
    problems = []

    def loads_it(path) -> bool:
        """Whether the game loads a file from the package; else the problem is said"""
        found = rescan.files.get(path)
        loaded = found.loaded if found else None
        if loaded is None:
            problems.append(f"{screen_label(path)}: the game doesn't see the merged patches")
        elif not is_package_folder(loaded.folder):
            problems.append(f"{screen_label(path)}: the game uses {loaded.folder}'s copy, which comes before the merged "
                            "patches. Give it a priority above 0 (in mods.settings or your mod manager)")
        else:
            return True
        return False

    for file in files:
        if file.status.included and loads_it(file.path) and rescan.files[file.path].winner != file.source:
            problems.append(f"{screen_label(file.path)}: {file.mod}'s copy changed while merging (or another mod's copy "
                            "is used now): merge again")
    for copy in as_is:
        if copy.entry is not None:
            loads_it(copy.path)
    return problems


def compat_notes(result: Scan, files: list[PackageFile]) -> list[str]:
    """For each installed compat patch whose every file the package holds: that it can be removed. (One the
    game still uses for a file the package doesn't hold, a screen the merger left out, gets no such note.)"""
    held = {file.path for file in files if file.status.included}
    return [f"{folder} is no longer needed (the merged patches replace it): you can remove it"
            for folder in result.removable_compat(held)]


@dataclass(slots=True)
class BuildResult:
    scan: Scan                      # before the build
    files: list[PackageFile]
    installation: Installation
    check_problems: list[str] = field(default_factory=list)     # see check_package; none with out_dir
    notes: list[str] = field(default_factory=list)              # see compat_notes

    @property
    def problems(self) -> list:
        """The scan's problems (see scan.Problem)"""
        return self.scan.problems

    @property
    def clean(self) -> bool:
        """Whether every file is patched as Smooth UI would, and nothing needs the user's attention"""
        return (not self.check_problems and not self.problems
                and all(file.severity is Level.OK for file in self.files)
                and not self.installation.written.unknown_files)


def build_package(game_path: Path, mods_settings: Path, *, force=(), out_dir: Path | None = None,
                  data_dir: Path | None = DATA_DIR, pool=None, on_file=print_file_result, progress=None,
                  **scan_options) -> BuildResult:
    """Scan, patch, decide (force: see policy.decide), install (out_dir and data_dir: see
    install) and check. scan_options go to scan.scan (for tests). Fails (SmoothUIError) if the installed
    Smooth UI build can't be used, or the package or mods.settings can't be written; problems with files
    or mods are in the result. progress(done, total), if given, is called in this thread once the number
    of files to patch is known, then as each file is done (after on_file)."""
    before = scan(game_path, mods_settings, **scan_options)
    total = sum(file.winner is not None for file in before.files.values())
    log.info(f"Merging {total} screen(s)...")
    done = 0

    def file_done(*args, **kwargs):
        nonlocal done
        done += 1
        on_file(*args, **kwargs)
        if progress:
            progress(done, total)

    if progress:
        progress(0, total)
    patch = patch_winners(before, pool=pool, on_file=file_done)
    files = decide(before, patch, force=force)
    as_is = as_is_copies(before, files)
    held = {copy.path for copy in as_is if copy.entry is not None}
    files = [replace(file, fallback=file.mod) if file.path in held else file for file in files]
    installation = install(before, files, mods_settings, out_dir=out_dir, data_dir=data_dir, as_is=as_is)
    # (With the same reviews: the update check may download new ones meanwhile, which the package wasn't made with)
    check_problems = [] if out_dir is not None else check_package(
        files, scan(game_path, mods_settings, **{**scan_options, "reviews": before.reviews}), as_is)
    return BuildResult(before, files, installation, check_problems, compat_notes(before, files))


def merge_report(result: BuildResult, started: str) -> dict:
    """A merge's full report, for the merger's report file (too long for its log): when it started (as the
    log shows it), and for each file its status, its issues, and the patcher's report (what was stretched
    or interpolated, what wasn't and why, the fixes) or its error"""
    return {
        "started": started,
        "smoothui": result.scan.installed.version,
        "files": {file.path: {"mod": file.mod, "status": file.status,
                              "issues": [issue.message for issue in file.issues], "error": file.error,
                              "report": file.result["report"] if file.result else None}
                  for file in result.files},
    }
