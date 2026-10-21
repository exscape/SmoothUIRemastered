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
from .package import Written, write_package
from .policy import PackageFile, decide
from .scan import PACKAGE_FOLDER, ProblemKind, Scan, is_package_folder, scan

log = logging.getLogger(__name__)

# mods.settings as it was before the merger last changed it
MODS_SETTINGS_BACKUP = DATA_DIR / "mods.settings.bak"


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
    entry_changed: bool | None      # whether mods.settings changed; None if the package went elsewhere


def install(result: Scan, files: list[PackageFile], mods_settings: Path, *, out_dir: Path | None = None,
            backup: Path | None = MODS_SETTINGS_BACKUP) -> Installation:
    """Write the package into the game's Mods\\ folder and give it its mods.settings entry (the file at
    mods_settings; its old content goes to backup first, if it changes). With out_dir, write the package
    there instead, for a look at it, and leave mods.settings alone."""
    package_dir = Path(out_dir or result.mods_dir) / PACKAGE_FOLDER
    written = write_package(package_dir, result.installed, files)
    if out_dir is not None:
        return Installation(package_dir, written, None)
    if backup is not None:
        make_dirs(Path(backup).parent)
    return Installation(package_dir, written, write_entry(mods_settings, PACKAGE_FOLDER, backup=backup))


def check_package(files: list[PackageFile], rescan: Scan) -> list[str]:
    """Problems with an installed package, from a scan made after it was installed: each file it holds must
    be loaded from it, and still come from the same copy (else a mod changed while it was built)."""
    if not rescan.installed.usable:
        return [f"the package can't be checked: {rescan.installed.message}"]
    problems = []
    for file in files:
        if not file.status.included:
            continue
        found = rescan.files.get(file.path)
        loaded = found.loaded if found else None
        if loaded is None:
            problems.append(f"{file.path}: the game doesn't load the package's copy (it doesn't see the package)")
        elif not is_package_folder(loaded.folder):
            problems.append(f"{file.path}: the game loads {loaded.folder}'s copy, not the package's: that mod comes "
                            "before the package in the load order. Change its priority to be a higher number than 0 (in mods.settings, or "
                            "in your mod manager)")
        elif found.winner != file.source:
            problems.append(f"{file.path}: {file.mod}'s copy changed (or another mod's copy now wins) while the package "
                            "was built: build it again")
    return problems


def compat_notes(result: Scan, files: list[PackageFile]) -> list[str]:
    """For each installed compat patch whose every file the package holds: that it can be removed. (One the
    game still needs for a file the package doesn't hold, e.g. for a mod that only ships .redswf files and
    is used through its compat patch alone, gets no such note.)"""
    held = {file.path for file in files if file.status.included}
    notes = []
    for problem in result.problems:
        if problem.kind != ProblemKind.COMPAT_INSTALLED:
            continue
        shipped = [key for key, file in result.files.items() if any(mod.folder == problem.mod for mod in file.shippers)]
        if all(key in held for key in shipped):
            notes.append(f"{problem.mod} is not needed while the package is used, and is safe to remove")
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
        """The scan's problems (see scan.Problem) that the user needs to see"""
        return [problem for problem in self.scan.problems if problem.kind != ProblemKind.COMPAT_INSTALLED]

    @property
    def clean(self) -> bool:
        """Whether every file is patched as Smooth UI would, and nothing needs the user's attention"""
        return (not self.check_problems and not self.problems
                and all(file.severity == "ok" for file in self.files)
                and not self.installation.written.unknown_files)


def build_package(game_path: Path, mods_settings: Path, *, force=(), leave_out=(), out_dir: Path | None = None,
                  pool=None, on_file=print_file_result, **scan_options) -> BuildResult:
    """Scan, patch, decide (force and leave_out: see policy.decide), install (out_dir: see install) and
    check. scan_options go to scan.scan (for tests). Fails (SmoothUIError) if the installed Smooth UI
    build can't be used, or the package or mods.settings can't be written; problems with files or mods are
    in the result."""
    before = scan(game_path, mods_settings, **scan_options)
    patch = patch_winners(before, pool=pool, on_file=on_file)
    files = decide(before, patch, force=force, leave_out=leave_out)
    installation = install(before, files, mods_settings, out_dir=out_dir)
    check_problems = [] if out_dir is not None else check_package(files, scan(game_path, mods_settings, **scan_options))
    return BuildResult(before, files, installation, check_problems, compat_notes(before, files))

