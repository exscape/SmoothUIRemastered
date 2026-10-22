"""How the merger writes files outside its own folder (the package in Mods\\, mods.settings): only ordinary
operations, each checked.

These rules come from MO2's virtual file system, where exists() and is_dir() often say no for files that
are there, Path.mkdir(exist_ok=True) fails, and exclusive creates misbehave.
They work the same without MO2, with any mod manager or none:
- folders are made one level at a time, with os.mkdir, and one that exists already is fine
- files are written with plain overwriting writes, under fixed names (no temporary name and rename),
  then read back
- a missing file is found by trying to open or delete it, never by checking first
"""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path

from .common import SmoothUIError, fail
from .windows import MB_ICONERROR, msg_box

# Windows' error for a file another process has open (ERROR_SHARING_VIOLATION), and for one that is
# locked (ERROR_LOCK_VIOLATION)
IN_USE_ERRORS = (32, 33)

# Set by stop_writes: from then on, make_dirs and write_file fail
_writes_stopped = threading.Event()


def stop_writes():
    """Make every later make_dirs and write_file fail, in every thread (one already writing finishes).
    For deleting what a build that can't be stopped otherwise (in another thread) has written."""
    _writes_stopped.set()


def make_dirs(path: Path):
    """Make a folder and any missing parents"""
    if _writes_stopped.is_set():
        fail(f"writing was stopped: {path} isn't created")
    path = Path(path)
    for folder in reversed([path, *path.parents]):
        if folder == folder.parent:
            continue    # the root
        try:
            os.mkdir(folder)
        except FileExistsError:
            pass
        except OSError as e:
            fail(f"can't create the folder {folder}: {e}")


def check_data_writable(data_dir: Path) -> bool:
    """Whether the tools' data folder can be written to; if not, says so in a message box. Both exes check
    this at every start, so that a tool put where Windows denies writes is noticed at once, not when the
    first mod needs patching."""
    probe = Path(data_dir) / "write-test.tmp"
    try:
        make_dirs(data_dir)
        probe.write_bytes(b"test")
        probe.unlink()
    except (SmoothUIError, OSError) as e:
        msg_box(f"Smooth UI's tools can't write to their data folder:\n\n{data_dir}\n\n{e}\n\nMove the whole "
                "Smooth UI Tools folder somewhere you can write to (not directly under C:\\Program Files), "
                "then start the tool again.", MB_ICONERROR)
        return False
    return True


def read_file(path: Path) -> bytes | None:
    """A file's content, or None if there is no such file"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        fail(f"can't read {path}: {_describe(e)}")


def write_file(path: Path, data: bytes):
    """Write a file, replacing any file there, and check it by reading it back"""
    if _writes_stopped.is_set():
        fail(f"writing was stopped: {path} isn't written")
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        fail(f"can't write {path}: {_describe(e)}")
    if read_file(path) != data:
        fail(f"{path} doesn't read back as written")


def delete_file(path: Path) -> bool:
    """Delete a file; returns whether there was one"""
    try:
        os.remove(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        fail(f"can't delete {path}: {_describe(e)}")
    return True


def remove_empty_dir(path: Path) -> bool:
    """Remove a folder if it is empty; anything else (not empty, can't be removed) leaves it as it is.
    Returns whether it is gone (also if it never was there)."""
    try:
        os.rmdir(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return True


def _describe(e: OSError) -> str:
    if getattr(e, "winerror", None) in IN_USE_ERRORS or isinstance(e, PermissionError) or e.errno == errno.EBUSY:
        return (f"{e.strerror or e}. Another program may be using it: most likely the game, so close it and try "
                "again; otherwise, a mod manager or an antivirus scan")
    return str(e.strerror or e)
