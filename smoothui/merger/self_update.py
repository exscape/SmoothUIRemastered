"""Updating the tools to a newer release (see updates.py for finding one). No Qt.

Every exe in the tools' folder shares _internal\\, so none of them can replace it while it runs. Hence a
helper: the new version's own merger, run from where it was unpacked.
1. prepare (in the merger's thread): download the zip to data\\update\\, check it, unpack it, and check that
   what it holds is the new version.
2. start_helper: SmoothUIMerger.exe --finish-update APP_DIR OLD_VERSION [--critical] PID... (from the
   unpacked folder), after which the merger exits. --critical: a critical update (updates.CRITICAL).
3. finish (the helper, first thing in gui.main, before anything is written; it holds the merger's mutex, so
   that a merger started meanwhile exits instead of deleting data\\update\\): wait for the PIDs to exit, then
   swap the files one at a time (see swap): the old version's move to data\\update\\old\\ (renames, so a
   failure can be undone), the new version's are copied in (the helper runs from them), release the mutex,
   start the new merger with --update-done OLD_VERSION (and --critical), and exit. Any failure puts the old version back, and says so in a
   message box.
4. cleanup (the new merger, soon after it starts): delete data\\update\\, retrying for a while because the
   helper may still be exiting. A start that finds data\\update\\ deletes it, whatever left it there.

Files the user keeps (data\\, anything the zip doesn't ship and no version of the tools did) are never
touched.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from ..common import SmoothUIError, fail
from ..launcher import UPDATING
from ..paths import APP_DIR, DATA_DIR
from ..windows import MB_ICONERROR, msg_box, release_mutexes, wait_for_process
from .gui_texts import UPDATE_NOT_INSTALLED, UPDATE_START_FAILED

if TYPE_CHECKING:
    from .updates import ToolsUpdate

log = logging.getLogger(__name__)

# In the data folder: the download, the unpacked version (UNPACKED/<version>), the old version's files
# (OLD), and the helper's log
UPDATE_DIR = "update"
OLD_DIR = "old"
HELPER_LOG = "update.log"

MERGER_EXE = "SmoothUIMerger.exe"
# Passed on by the helper to the new merger: the update was a critical one (its card says so)
CRITICAL_ARG = "--critical"
REQUIRED = (MERGER_EXE, "SmoothUILauncher.exe", "_internal")
VERSION_FILE = Path("_internal") / "smoothui" / "merger" / "tools_version.txt"

# What the tools' folder holds besides the user's data\ (the names of every version so far): what an update
# replaces even if the new version no longer ships it. Whatever else is in the folder is left alone.
SHIPPED_NAMES = (MERGER_EXE, "SmoothUILauncher.exe", "_internal", "manifests", "licenses", "LICENSE.txt",
                 "README_tools.md", "smoothui.ico", "smoothui-launcher.ico")
KEPT_NAMES = ("data",)

MAX_UNPACKED = 1024 * 1024 * 1024   # the zip is ~100 MB unpacked: more than this isn't ours
PID_TIMEOUT = 30                    # s to wait for each process to exit
RETRY_FOR = 10                      # s to retry a file's move, copy or delete (an antivirus scan may hold it)
CLEANUP_FOR = 10                    # s to retry the cleanup (the helper is still exiting)

# Held while data\update\ is being deleted: a download waits for it
_cleaning = threading.Lock()


class UpdateFailed(SmoothUIError):
    """The update didn't happen, and the old version is as it was"""


# ---- In the merger: download, unpack, start the helper ----

def prepare(update: ToolsUpdate, data_dir: Path = DATA_DIR, progress=None, timeout: float | None = None) -> Path:
    """Download the release's zip into data_dir\\update\\ and unpack it; returns the folder with the new
    SmoothUIMerger.exe. progress is called with (bytes so far, total). Fails (SmoothUIError) with a text for
    the user."""
    from . import updates  # (imported only when updating)
    with _cleaning:  # (until a cleanup has ended)
        folder = Path(data_dir) / UPDATE_DIR
        _remove(folder)
        if os.path.lexists(folder):
            fail(f"can't clear the folder {folder} for the download: another program may be using something in it")
        try:
            os.makedirs(folder)
            zip_path = folder / f"{update.version}.zip"
            part = zip_path.with_name(zip_path.name + ".part")
            updates.download(update.asset, part, timeout or updates.TIMEOUT, progress)
            os.replace(part, zip_path)
            root = unpack(zip_path, folder / update.version, update.version)
            zip_path.unlink()
        except OSError as e:
            fail(f"can't write to {folder}: {e.strerror or e}")
        return root


def unpack(zip_path: Path, to: Path, version: str) -> Path:
    """Unpack a tools zip into the (new) folder to, and return the folder holding SmoothUIMerger.exe (the zip's
    root, or its one folder). Refuses a zip with a path that leaves the folder, and one that isn't the
    wanted version of the tools."""
    import zipfile  # (imported only when updating: ~12 ms)
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            for info in infos:
                if not safe_name(info.filename):
                    fail(f"the download holds a file with an unsafe name ({info.filename})")
            if sum(info.file_size for info in infos) > MAX_UNPACKED:
                fail("the download is much larger unpacked than it should be")
            os.makedirs(to)
            z.extractall(to)
    except zipfile.BadZipFile as e:
        fail(f"the download isn't a zip file ({e})")
    root = tools_root(to)
    missing = [name for name in REQUIRED if not os.path.lexists(root / name)]
    if missing:
        fail(f"the download lacks {', '.join(missing)}")
    try:
        found = (root / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        fail("the download doesn't say which version of the tools it is")
    if found != version:
        fail(f"the download is version {found}, not {version}")
    return root


def safe_name(name: str) -> bool:
    """Whether a zip entry's path stays inside the folder it's unpacked to"""
    if not name or "\\" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and not PureWindowsPath(name).drive and ".." not in path.parts


def tools_root(folder: Path) -> Path:
    """The folder holding SmoothUIMerger.exe: this one, or the only folder in it"""
    if (folder / MERGER_EXE).exists():
        return folder
    inside = [p for p in folder.iterdir() if p.is_dir()]
    if len(inside) == 1 and (inside[0] / MERGER_EXE).exists():
        return inside[0]
    fail(f"the download has no {MERGER_EXE}")


def start_helper(root: Path, app_dir: Path, old_version: str, pids: list[int], critical: bool = False):
    """Start the new version's merger as the helper (it outlives this process). The caller then exits."""
    command = [str(root / MERGER_EXE), "--finish-update", str(app_dir), old_version,
               *([CRITICAL_ARG] if critical else []), *map(str, pids)]
    log.info(f"Starting the update helper: {subprocess.list2cmdline(command)}")
    try:
        _start(command, root)
    except OSError as e:
        fail(f"can't start the new version: {e.strerror or e}")


def exit_code(stale: bool) -> int:
    """What the merger exits with once the helper runs: in stale mode, the launcher must not start the game"""
    return UPDATING if stale else 0


# ---- The helper ----

def finish(args: list[str], new_root: Path = APP_DIR) -> int:
    """The helper's main: args are APP_DIR OLD_VERSION [--critical] PID...; new_root: where the helper itself
    runs from (the unpacked new version). Returns its exit code."""
    critical = CRITICAL_ARG in args
    args = [arg for arg in args if arg != CRITICAL_ARG]
    app_dir, old_version, pids = Path(args[0]), args[1], [int(pid) for pid in args[2:]]
    update_dir = app_dir / DATA_DIR.name / UPDATE_DIR
    _log_to(update_dir / HELPER_LOG)
    log.info(f"Finishing the update from {old_version}: {app_dir}; waiting for {pids}")
    try:
        for pid in pids:
            if not wait_for_process(pid, PID_TIMEOUT):
                raise UpdateFailed(f"the process {pid} didn't exit")
        swap(new_root, app_dir, update_dir / OLD_DIR)
    except SmoothUIError as e:
        log.error(f"The update failed: {e}")
        msg_box(UPDATE_NOT_INSTALLED.format(error=e), MB_ICONERROR)
        return 1
    log.info("The new version is installed: starting it")
    release_mutexes()   # (the merger's, held since the helper started: the new merger takes it)
    try:
        _start([str(app_dir / MERGER_EXE), "--update-done", old_version, *([CRITICAL_ARG] if critical else [])],
               app_dir)
    except OSError as e:
        log.error(f"Can't start the new version: {e}")
        msg_box(UPDATE_START_FAILED.format(folder=app_dir, error=e.strerror or e), MB_ICONERROR)
        return 1
    return 0


def swap(new_root: Path, app_dir: Path, old_dir: Path, retry_for: float = RETRY_FOR):
    """Replace the tools in app_dir with those in new_root: every file under the names the new version ships
    (or any version has shipped) moves to old_dir, then the new version's files are copied in, and the
    folders left empty are deleted. On a failure, everything is put back as it was, and this fails
    (UpdateFailed).

    One file at a time, never a whole folder: Windows refuses to rename a folder while any program has
    anything in it open, even just to watch it (e.g. an editor showing a file in it), whereas a file can be
    moved unless that file itself is open, even while a program runs from it."""
    # In any case, as Windows compares names (e.g. a Data\ in the zip must not take the user's data\);
    # spelled as the new version spells them
    kept = {name.casefold() for name in KEPT_NAMES}
    names = sorted({name.casefold(): name for name in (*SHIPPED_NAMES, *(p.name for p in new_root.iterdir()))
                    if name.casefold() not in kept}.values())
    old_files = [file for name in names for file in _files(app_dir, name)]
    new_files = [file for name in names for file in _files(new_root, name)]
    if os.path.lexists(old_dir):
        raise UpdateFailed(f"{old_dir} already exists")
    moved, copied = [], []
    try:
        for file in old_files:
            _retry(lambda file=file: _move(app_dir / file, old_dir / file), retry_for,
                   f"can't move {app_dir / file} out of the way")
            moved.append(file)
        for file in new_files:
            copied.append(file)     # (before the copy: a half-made copy must go too)
            _retry(lambda file=file: _copy(new_root / file, app_dir / file), retry_for, f"can't copy {file}")
    except UpdateFailed as e:
        log.error(f"{e}: putting the old version back")
        try:
            for file in reversed(copied):
                _remove(app_dir / file, retry_for)
                if os.path.lexists(app_dir / file):
                    raise UpdateFailed(f"can't remove the new {file}")
            for file in reversed(moved):
                _retry(lambda file=file: _move(old_dir / file, app_dir / file), retry_for, f"can't move {file} back")
        except UpdateFailed as e2:
            log.error(str(e2))
            raise UpdateFailed(f"{e}. And the old version couldn't be put back ({e2}): download Smooth UI Tools "
                               f"again from Nexus Mods, or move what is in {old_dir} back to {app_dir}") from e
        raise
    finally:
        _remove_empty_folders(app_dir, names)


def _files(folder: Path, name: str) -> list[Path]:
    """The files in folder\\name (a folder or a file), relative to folder; none if it doesn't exist"""
    path = folder / name
    if path.is_dir() and not path.is_symlink():
        return sorted(Path(top).relative_to(folder) / file for top, _, files in os.walk(path) for file in files)
    return [Path(name)] if os.path.lexists(path) else []


def _move(source: Path, target: Path):
    os.makedirs(target.parent, exist_ok=True)
    os.rename(source, target)


def _copy(source: Path, target: Path):
    os.makedirs(target.parent, exist_ok=True)
    shutil.copyfile(source, target)


def _remove_empty_folders(folder: Path, names):
    """Delete the empty folders in folder\\name, for each name (what a swap emptied); one in use stays"""
    for name in names:
        for top, _, _ in os.walk(folder / name, topdown=False):
            try:
                os.rmdir(top)
            except OSError:
                pass    # not empty, or in use


def _retry(action, seconds: float, what: str):
    """Do action(), again for up to seconds while it fails with an OSError (a file in use); fails with what"""
    end = time.monotonic() + seconds
    while True:
        try:
            return action()
        except OSError as e:
            if time.monotonic() >= end:
                raise UpdateFailed(f"{what}: {e.strerror or e}. Another program may be using it") from e
            time.sleep(0.25)


# ---- Afterwards ----

def start_cleanup(data_dir: Path = DATA_DIR):
    """Delete data_dir\\update\\ in a thread, if there is one (see cleanup), and log the helper's log"""
    if os.path.lexists(Path(data_dir) / UPDATE_DIR):
        threading.Thread(target=cleanup, args=(data_dir,), daemon=True).start()


def cleanup(data_dir: Path = DATA_DIR, retry_for: float = CLEANUP_FOR):
    """Delete data_dir\\update\\, for up to retry_for s (the helper's exe may still be running); what is
    left is deleted by a later start. The helper's log goes to this process's first."""
    folder = Path(data_dir) / UPDATE_DIR
    with _cleaning:
        try:
            for line in (folder / HELPER_LOG).read_text(encoding="utf-8").splitlines():
                log.info(f"(update helper) {line}")
        except OSError:
            pass
        _remove(folder, retry_for)
    if os.path.lexists(folder):
        log.warning(f"{folder} couldn't be deleted completely (something in it is in use); the next start tries again")
    else:
        log.info(f"Deleted {folder}")


def _remove(path: Path, retry_for: float = 0):
    """Delete a file or folder, for up to retry_for s; whatever remains, remains"""
    end = time.monotonic() + retry_for
    while os.path.lexists(path):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
        if not os.path.lexists(path) or time.monotonic() >= end:
            return
        time.sleep(0.25)


def _log_to(path: Path):
    """The helper's log: a file in data\\update\\ (there is no console, and its own data folder is not made)"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        os.makedirs(path.parent, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root.addHandler(handler)


def _start(command: list[str], cwd: Path):
    """Start a program that keeps running after this process ends (on Windows: detached, in a process group
    of its own)"""
    detached, new_group = 0x8, 0x200
    flags = detached | new_group if sys.platform == "win32" else 0
    subprocess.Popen(command, cwd=cwd, close_fds=True, creationflags=flags)
