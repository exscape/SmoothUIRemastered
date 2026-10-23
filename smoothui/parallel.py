"""Process pools for CPU-bound work (patching files), and their logging.

The patcher is pure Python, so threads would run one at a time (the GIL); each file is patched in a worker
process instead. Workers are started with "spawn" everywhere: Windows (and so the frozen tool) has nothing
else, and the command line under WSL then behaves the same. Frozen exes must call
multiprocessing.freeze_support() first in main.

A worker's log records are collected per task and returned with its result (see run_logged), so the main
process can replay each file's output in one piece (replay_records), as each task is done. The records then
get the time they are replayed at, so that a log reads in order; the worker's own time for the task is
logged at its end.

On Windows, the workers end with the process that started them, however it ends (see _workers_job).
"""

import importlib
import logging
import logging.handlers
import multiprocessing
import os
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor

log = logging.getLogger(__name__)

# Undocumented: SMOOTHUI_WORKERS=1 makes callers work in-process (for comparing a parallel build with a
# serial one, debugging and profiling); any other number sets the worker count
WORKERS_VARIABLE = "SMOOTHUI_WORKERS"

# GetLogicalProcessorInformationEx: one record per physical core
RELATION_PROCESSOR_CORE = 0

# Job objects (Windows)
JOB_OBJECT_ASSIGN_PROCESS = 0x0001
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# More workers stop helping here: it saves no time, but adds 50-120 MiB of RAM usage per extra worker.
MAX_WORKERS = 4


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
    return max(1, min(physical_cores(), MAX_WORKERS, tasks))


def make_pool(workers: int) -> ProcessPoolExecutor:
    """A pool whose workers log like this process does (see run_logged). Callers that run several
    builds (the GUI) can keep one and pass it in."""
    root = logging.getLogger()
    levels = {name: logger.level for name, logger in root.manager.loggerDict.items()
              if isinstance(logger, logging.Logger) and logger.level}
    levels[""] = root.level
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_init_worker, initargs=(levels, _workers_job()))


_job = None     # (name, handle) of the job _workers_job made, once


def _workers_job() -> str | None:
    """The name of a job object that Windows ends together with this process: it kills the processes in the
    job when the job's last handle closes, and that handle is this process's, closed when it ends, however it
    ends (also from Task Manager, or by a crash). Without it, a worker whose main process is gone waits for
    work forever (it holds both ends of its pipes, so it never notices), and a mod manager sees the program
    still running. Only the workers join it (_init_worker): this process stays out of it, and so does
    anything else it starts (a browser, Explorer, or the game, which must never end with the tools). None
    outside Windows, or if it can't be made: workers then end only as before (see kill_workers)."""
    global _job
    if _job is None and sys.platform == "win32":
        name = f"Local\\SmoothUI-workers-{os.getpid()}"
        try:
            _job = (name, _make_job(name))
            log.debug(f"Workers end with this process (job object {name})")
        except OSError as e:
            _job = (None, None)
            log.debug(f"Workers may outlive this process: no job object ({e})")
    return _job[0] if _job else None


def _make_job(name):
    """A job object that kills its processes when its last handle closes; returns that handle. (Handles are
    passed as c_void_p: a plain int would be passed as a 32-bit C int.)"""
    import ctypes

    class Limits(ctypes.Structure):     # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, its first member's fields inlined
        _fields_ = [("UserTimeLimits", ctypes.c_int64 * 2), ("LimitFlags", ctypes.c_uint32),
                    ("WorkingSetSizes", ctypes.c_size_t * 2), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityAndSchedulingClass", ctypes.c_uint32 * 2),
                    ("IoInfo", ctypes.c_uint64 * 6), ("MemoryLimits", ctypes.c_size_t * 4)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, name)
    limits = Limits(LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    if not job or not kernel32.SetInformationJobObject(ctypes.c_void_p(job), JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                       ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    return job     # never closed: Windows closes it when this process ends


def _join_job(name):
    """In a worker: join the job _workers_job named, closing its own handle at once, so that the main
    process's stays the last one. A failure is ignored (the worker then ends only as before)."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenJobObjectW.restype = ctypes.c_void_p
    if job := kernel32.OpenJobObjectW(JOB_OBJECT_ASSIGN_PROCESS, False, name):
        kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(-1))    # -1: this process
        kernel32.CloseHandle(ctypes.c_void_p(job))


def kill_workers(pool: ProcessPoolExecutor):
    """End a pool's workers at once, even in the middle of a task: for a process that exits without waiting
    for them (they'd go on otherwise, and a mod manager would see the program still running)"""
    if hasattr(pool, "kill_workers"):   # Python 3.14
        pool.kill_workers()
        return
    for process in list((pool._processes or {}).values()):
        process.kill()


class _RecordList(list):
    """A "queue" for QueueHandler that keeps the records"""
    def put_nowait(self, record):
        self.append(record)


_records = _RecordList()


def _init_worker(levels, job=None):
    """In each worker: join the job that ends it with the main process (see _workers_job), the main
    process's logger levels, and every record collected into _records"""
    if job:
        _join_job(job)
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.addHandler(logging.handlers.QueueHandler(_records))   # also makes records picklable
    for name, level in levels.items():
        logging.getLogger(name or None).setLevel(level)


def run_logged(function, *args):
    """Run function(*args) in a worker (pool.submit(run_logged, function, ...)). Returns (its result,
    the log records it made, the last one with the time it took)."""
    _records.clear()
    start = time.perf_counter()
    try:
        result = function(*args)
        log.debug(f"Done in {time.perf_counter() - start:.2f} s (worker process {os.getpid()})")
        return result, list(_records)
    finally:
        _records.clear()


def replay_records(records):
    """Log a worker's records in this process, as if they had been logged here, now"""
    now = time.time()
    for record in records:
        record.created = now
        record.msecs = (now - int(now)) * 1000
        logging.getLogger(record.name).handle(record)


def warm_up(pool: ProcessPoolExecutor, workers: int, module: str):
    """Start the pool's workers now, each importing module (what its tasks will need), so that the first
    tasks don't wait for that. (A pool starts a worker only when a task finds none free.)"""
    for _ in range(workers):
        pool.submit(_import, module)


def _import(module):
    importlib.import_module(module)     # returns nothing: a module can't be sent back
