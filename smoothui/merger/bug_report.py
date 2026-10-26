"""The bug reports (the merger window's Bug report): a short text the user reads, edits and pastes into their
report (short_report), and a zip for when more is needed (save_bug_report). The zip in the data folder's bug-reports\\ with what it
takes to see what the merger did and why: its logs and merge reports (and the launcher's logs), its settings,
the package manifest and the records of packages that hold no files, mods.settings and the merger's backup
of it, and a summary (versions, the release manifests the merger has, Windows' version, the load order of
the mods that ship Smooth UI's screens).

The user's profile folder (C:\\Users\\<name>) is replaced with %USERPROFILE% in every file, so that the report
doesn't say whose PC it comes from. Light: no Qt, and none of the patcher.
"""

from __future__ import annotations

import io
import os
import platform
import re
import time
import zipfile
from pathlib import Path

from ..common import SmoothUIError
from ..files import make_dirs, read_file, write_file
from ..manifests import load_manifest, release_manifest_paths
from ..paths import FROZEN
from . import tools_version
from .game import MODS_SETTINGS_BACKUP, SETTINGS_FILE
from .package import PACKAGE_MANIFEST, RECORDS
from .scan import PACKAGE_FOLDER
from .state import Check, State

FOLDER = "bug-reports"
# The logs and merge reports in the data folder, of every generation kept (see files.rotate)
LOGS = re.compile(r"(merger|launcher)(\.\d+)?\.(log|report\.json)", re.IGNORECASE)
PROFILE = "%USERPROFILE%"
LOG_LINES, LOG_LINE_LENGTH = 15, 300     # the short report's share of the log


def save_bug_report(data_dir: Path, game: Path | None, mods_settings: Path | None, found: Check | None) -> Path:
    """Write a bug report into data_dir's FOLDER; returns its path. game, mods_settings: where the game and
    mods.settings are (None: not known); found: the last check (None if there is none). A file that can't
    be read is left out, and the summary says why. Fails (SmoothUIError) only if the zip can't be written."""
    data_dir = Path(data_dir)
    files = {}      # name in the zip: path
    for name in sorted(os.listdir(data_dir)):
        if LOGS.fullmatch(name):
            files[f"data/{name}"] = data_dir / name
    files[f"data/{SETTINGS_FILE}"] = data_dir / SETTINGS_FILE
    files[f"data/{MODS_SETTINGS_BACKUP}"] = data_dir / MODS_SETTINGS_BACKUP
    try:
        for name in sorted(os.listdir(data_dir / RECORDS)):
            files[f"data/{RECORDS}/{name}"] = data_dir / RECORDS / name
    except OSError:
        pass        # no records
    if mods_settings is not None:
        files["mods.settings"] = Path(mods_settings)
    mods_dir = found.scan.mods_dir if found is not None else Path(game, "Mods") if game is not None else None
    if mods_dir is not None:
        files[PACKAGE_MANIFEST] = mods_dir / PACKAGE_FOLDER / PACKAGE_MANIFEST

    profile = os.environ.get("USERPROFILE")
    unread = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in files.items():
            try:
                data = read_file(path)
            except SmoothUIError as e:
                unread.append(f"{name}: {e}")
                continue
            if data is not None:
                archive.writestr(name, redact(data, profile))
        summary = "\n".join(_summary(game, mods_settings, found, unread)) + "\n"
        archive.writestr("summary.txt", redact(summary.encode("utf-8"), profile))
    folder = data_dir / FOLDER
    make_dirs(folder)
    path = folder / f"SmoothUI-report-{time.strftime('%Y-%m-%d-%H%M%S')}.zip"
    write_file(path, buffer.getvalue())
    return path


def redact(data: bytes, profile: str | None) -> bytes:
    """data with the profile folder replaced by PROFILE: as it is, with forward slashes, and with doubled
    backslashes (as in JSON); in any case, and only where the folder's name ends (not C:\\Users\\bob in
    C:\\Users\\bobby). Bytes that aren't UTF-8 stay as they are."""
    profile = (profile or "").rstrip("\\/")
    if not profile:
        return data
    forms = {profile, profile.replace("\\", "/"), profile.replace("\\", "\\\\")}
    pattern = "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
    text = data.decode("utf-8", "surrogateescape")
    text = re.sub(f"(?:{pattern})(?![\\w-]|\\.\\w)", PROFILE, text, flags=re.IGNORECASE)
    return text.encode("utf-8", "surrogateescape")


def _summary(game, mods_settings, found: Check | None, unread) -> list[str]:
    try:
        version = tools_version()
    except SmoothUIError as e:
        version = f"unknown ({e})"
    lines = [
        f"Smooth UI bug report, saved {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Smooth UI Tools v{version}" + ("" if FROZEN else " (run from source)"),
        f"Windows: {platform.platform()}",
        f"Game folder: {game or 'not chosen yet'}",
        f"mods.settings: {mods_settings or 'unknown'}",
    ]
    if found is None:
        lines.append("Smooth UI: not checked (no scan has ended, or it failed: see merger.log)")
    else:
        installed = found.scan.installed
        lines.append(f"Smooth UI: {installed.status.value}, {installed.message}")
        if installed.manifest is not None:
            lines.append(f"Its release manifest was created at {installed.manifest.get('created_at')}")
    lines += ["", "Release manifests the merger has:"]
    for release, path in sorted(release_manifest_paths().items()):
        try:
            created = (load_manifest(path) or {}).get("created_at")
        except (OSError, ValueError) as e:
            created = f"unreadable ({e})"
        lines.append(f"  {release}, created at {created} ({path})")
    if found is not None:
        lines += ["", *_load_order(found)]
    if unread:
        lines += ["", "Files that can't be read:", *(f"  {line}" for line in unread)]
    return lines


def _load_order(found: Check) -> list[str]:
    """The mods that ship Smooth UI's screens, in load order"""
    lines = ["Mods that ship Smooth UI's screens, in load order:"]
    shippers = {mod for file in found.scan.files.values() for mod in file.shippers}
    for mod in (mod for mod in found.scan.order if mod in shippers):
        settings = mod.settings
        where = ("no mods.settings entry" if settings is None
                 else f"Priority {settings.priority}" if settings.priority is not None else "no Priority")
        lines.append(f"  {mod.folder} ({where})")
    return lines


def short_report(game: Path | None, found: Check | None, window_lines: list[str], log_path: Path) -> str:
    """The short bug report as text for the user to read, edit and paste: the versions, what the window shows
    (window_lines), the load order, the last warnings and errors in the log at log_path, and the mods whose
    screens couldn't be fully merged, for the user to add their versions. Nothing else is in it, and the
    profile folder is replaced as in the zip."""
    try:
        version = tools_version()
    except SmoothUIError as e:
        version = f"unknown ({e})"
    lines = [
        f"Smooth UI Tools v{version}" + ("" if FROZEN else " (run from source)"),
        f"Windows: {platform.platform()}",
        f"Game folder: {game or 'not chosen yet'}",
    ]
    if found is not None:
        lines.append(f"Smooth UI: {found.scan.installed.message}")
    if window_lines:
        lines += ["", "What the merger's window shows:", *(f"  {line}" for line in window_lines)]
    if found is not None:
        lines += ["", *_load_order(found)]
    problems = _log_problems(log_path)
    if problems:
        lines += ["", "Warnings and errors in the log:", *(f"  {line}" for line in problems)]
    if found is not None:
        lines += _not_fully_merged(found)
    return redact("\n".join(lines).encode("utf-8"), os.environ.get("USERPROFILE")).decode("utf-8", "replace") + "\n"


def _not_fully_merged(found: Check) -> list[str]:
    """The mods whose copies the merged patches leave out, or hold anyway or without some fixes, each with
    blanks for the user to fill in (their versions aren't on disk)"""
    mods = {}
    for file in found.states:
        if file.winner and ((file.state == State.NOT_HELD and file.cause in ("left out", "failed", "mod failed"))
                            or (file.state == State.UP_TO_DATE and (file.cause == "forced" or file.fixes))):
            mods.setdefault(file.winner.lower(), file.winner)
    if not mods:
        return []
    return ["", "Mods whose screens couldn't be fully merged (please fill in):",
            *(f"  {mod}: version ___, Nexus link ___" for mod in sorted(mods.values(), key=str.lower))]


def _log_problems(path: Path) -> list[str]:
    """The last LOG_LINES warning and error lines of the log (cut at LOG_LINE_LENGTH)"""
    try:
        text = read_file(path)
    except SmoothUIError:
        return []
    lines = [line for line in (text or b"").decode("utf-8", "replace").splitlines()
             if re.match(r"\S+ \S+ (WARNING|ERROR|CRITICAL) ", line)]
    return [line[:LOG_LINE_LENGTH] for line in lines[-LOG_LINES:]]
