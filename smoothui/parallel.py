"""Process pools for CPU-bound work (patching files), and their logging.

The patcher is pure Python, so threads would run one at a time (the GIL); each file is patched in a worker
process instead. Workers are started with "spawn" everywhere: Windows (and so the frozen tool) has nothing
else, and the command line under WSL then behaves the same. Frozen exes must call
multiprocessing.freeze_support() first in main.

A worker's log records are collected per task and returned with its result (see run_logged), so the main
process can replay each file's output in one piece and in order (replay_records).
"""

import logging
import logging.handlers
import multiprocessing
import os
import struct
import sys
from concurrent.futures import ProcessPoolExecutor

# Undocumented: SMOOTHUI_WORKERS=1 makes callers work in-process (for comparing a parallel build with a
# serial one, debugging and profiling); any other number sets the worker count
WORKERS_VARIABLE = "SMOOTHUI_WORKERS"

# GetLogicalProcessorInformationEx: one record per physical core
RELATION_PROCESSOR_CORE = 0


def physical_cores() -> int:
    """The number of physical cores; os.cpu_count() if unknown"""
    try:
        count = _windows_physical_cores() if sys.platform == "win32" else _linux_physical_cores()
    except (OSError, ValueError):
        count = None
    return count or os.cpu_count() or 1


def _windows_physical_cores():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    length = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, None, ctypes.byref(length))
    buffer = ctypes.create_string_buffer(length.value)
    if not kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, buffer, ctypes.byref(length)):
        return None
    # Each record starts with its Relationship and Size (two DWORDs)
    count = offset = 0
    while offset < length.value:
        offset += struct.unpack_from("<I", buffer, offset + 4)[0]
        count += 1
    return count


def _linux_physical_cores():
    """The distinct (physical id, core id) pairs in /proc/cpuinfo (also in WSL)"""
    cores, physical_id = set(), None
    with open("/proc/cpuinfo", encoding="utf-8") as f:
        for line in f:
            key, _, value = line.partition(":")
            key = key.strip()
            if key == "physical id":
                physical_id = value.strip()
            elif key == "core id":
                cores.add((physical_id, value.strip()))
    return len(cores)


def worker_count(tasks: int) -> int:
    """How many workers to use for this many tasks: 1 means work in-process, without a pool"""
    forced = os.environ.get(WORKERS_VARIABLE)
    if forced:
        try:
            return max(1, min(int(forced), tasks))
        except ValueError:
            raise ValueError(f"{WORKERS_VARIABLE} must be a number, not {forced!r}") from None
    return max(1, min(physical_cores(), tasks))


def make_pool(workers: int) -> ProcessPoolExecutor:
    """A pool whose workers log like this process does (see run_logged). Callers that run several
    builds (the GUI) can keep one and pass it in."""
    root = logging.getLogger()
    levels = {name: logger.level for name, logger in root.manager.loggerDict.items()
              if isinstance(logger, logging.Logger) and logger.level}
    levels[""] = root.level
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_init_worker, initargs=(levels,))


class _RecordList(list):
    """A "queue" for QueueHandler that keeps the records"""
    def put_nowait(self, record):
        self.append(record)


_records = _RecordList()


def _init_worker(levels):
    """In each worker: the main process's logger levels, and every record collected into _records"""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.addHandler(logging.handlers.QueueHandler(_records))   # also makes records picklable
    for name, level in levels.items():
        logging.getLogger(name or None).setLevel(level)


def run_logged(function, *args):
    """Run function(*args) in a worker (pool.submit(run_logged, function, ...)). Returns (its result,
    the log records it made)."""
    _records.clear()
    try:
        return function(*args), list(_records)
    finally:
        _records.clear()


def replay_records(records):
    """Log a worker's records in this process, as if they had been logged here"""
    for record in records:
        logging.getLogger(record.name).handle(record)
