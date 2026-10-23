"""Building the package: scan (scan.py) -> patch the winning copies (patch_winners) -> choose what goes in
(policy.decide) -> write it (install) -> check that the game loads it (check_package, on a new scan).

build_package runs them all. A GUI can run them one by one instead: show the scan, patch once, then call
decide again whenever the user changes a choice, and install when they are done.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..build.release import ReleasePatch, patch_as_release, profile_from_manifest
from ..build.reviewed_warnings import WarningSummary
from ..build.shared import print_file_result
from ..common import fail, profile_key
from ..files import make_dirs
from ..patcher.patcher_core import RuleIssues
from ..paths import DATA_DIR
from ..witcher3.mods_settings import write_entry
from .package import Written, record_path, write_package
from .policy import PackageFile, decide
from .scan import PACKAGE_FOLDER, Scan, is_package_folder, scan
from .screens import screen_label

log = logging.getLogger(__name__)

# mods.settings as it was before the merger last changed it, in the merger's data folder
MODS_SETTINGS_BACKUP = "mods.settings.bak"


def patch_winners(result: Scan, *, pool=None, on_file=print_file_result) -> ReleasePatch:
    """Patch the winning copy of each file (see scan.FileResult.winner) with the installed Smooth UI
    build's settings. pool and on_file are as for patch_as_release."""
    installed = result.installed
    if not installed.usable:
        fail(installed.message)
    sources = [file.winner for file in result.files.values() if file.winner]
    if not sources:
        return ReleasePatch([], [], [], WarningSummary({}, 0, [], 0), RuleIssues([], []))
    profile = {profile_key(key): settings for key, settings in profile_from_manifest(installed.manifest).items()}
    return patch_as_release(sources, {source.path: profile[source.path] for source in sources}, installed.manifest,
                            on_file=on_file, pool=pool)


@dataclass(slots=True)
class Installation:
    """What install returns"""
    package_dir: Path
    written: Written
    entry_changed: bool | None      # whether mods.settings changed; None if it was left alone: the package
                                    # went elsewhere, or holds no files


def install(result: Scan, files: list[PackageFile], mods_settings: Path, *, out_dir: Path | None = None,
            data_dir: Path | None = DATA_DIR) -> Installation:
    """Write the package into the game's Mods\\ folder and give it its mods.settings entry (the file at
    mods_settings; its old content goes to MODS_SETTINGS_BACKUP in data_dir first, if it changes). With
    out_dir, write the package there instead, for a look at it, and leave mods.settings alone. A package
    that holds no files is removed instead, with a record in data_dir (see package.py), and mods.settings
    is left alone: an entry it has stays, as a later build would write it again. data_dir None: no
    backup and no record."""
    package_dir = Path(out_dir or result.mods_dir) / PACKAGE_FOLDER
    record = record_path(data_dir, result.mods_dir) if data_dir is not None and out_dir is None else None
    written = write_package(package_dir, result.installed, files, record)
    if out_dir is not None or written.empty:
        return Installation(package_dir, written, None)
    backup = None
    if data_dir is not None:
        make_dirs(data_dir)
        backup = Path(data_dir) / MODS_SETTINGS_BACKUP
    return Installation(package_dir, written, write_entry(mods_settings, PACKAGE_FOLDER, backup=backup))


def check_package(files: list[PackageFile], rescan: Scan) -> list[str]:
    """Problems with an installed package, from a scan made after it was installed: each file it holds must
    be loaded from it, and still come from the same copy (else a mod changed while it was built)."""
    if not rescan.installed.usable:
        return [f"the merged patches can't be checked: {rescan.installed.message}"]
    problems = []
    for file in files:
        if not file.status.included:
            continue
        found = rescan.files.get(file.path)
        loaded = found.loaded if found else None
        label = screen_label(file.path)
        if loaded is None:
            problems.append(f"{label}: the game doesn't see the merged patches")
        elif not is_package_folder(loaded.folder):
            problems.append(f"{label}: the game uses {loaded.folder}'s copy, which comes before the merged patches. "
                            "Give it a priority above 0 (in mods.settings or your mod manager)")
        elif found.winner != file.source:
            problems.append(f"{label}: {file.mod}'s copy changed while merging (or another mod's copy is used now): "
                            "merge again")
    return problems


def compat_notes(result: Scan, files: list[PackageFile]) -> list[str]:
    """For each installed compat patch whose every file the package holds: that it can be removed. (One the
    game still needs for a file the package doesn't hold, e.g. for a mod that only ships .redswf files and
    is used through its compat patch alone, gets no such note.)"""
    held = {file.path for file in files if file.status.included}
    notes = []
    for folder in result.compat_patches:
        shipped = [key for key, file in result.files.items() if any(mod.folder == folder for mod in file.shippers)]
        if all(key in held for key in shipped):
            notes.append(f"{folder} is no longer needed (the merged patches replace it): you can remove it")
    return notes


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
                and all(file.severity == "ok" for file in self.files)
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
    installation = install(before, files, mods_settings, out_dir=out_dir, data_dir=data_dir)
    check_problems = [] if out_dir is not None else check_package(files, scan(game_path, mods_settings, **scan_options))
    return BuildResult(before, files, installation, check_problems, compat_notes(before, files))


def merge_report(result: BuildResult, started: str) -> dict:
    """A merge's full report, for the merger's report file (too long for its log): when it started (as the
    log shows it), and for each file its status, its issues, and the patcher's report (what was stretched
    or interpolated, what wasn't and why, the fixes) or its error"""
    return {
        "started": started,
        "smoothui": result.scan.installed.version,
        "files": {file.path: {"mod": file.mod, "status": file.status.value,
                              "issues": [issue.message for issue in file.issues], "error": file.error,
                              "report": file.result["report"] if file.result else None}
                  for file in result.files},
    }
