"""What both exes need from Windows itself: message boxes, and when the process started. Light (ctypes
only); elsewhere (tests under WSL), the boxes show nothing and the start time is unknown."""

import ctypes

TITLE = "Smooth UI Remastered"

MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000

PROCESS_PER_MONITOR_DPI_AWARE = 2


def msg_box(text: str, flags: int = MB_ICONWARNING):
    """Show a message box and wait for the user to close it. Never raises."""
    try:
        # Without this, Windows scales the box up on HiDPI monitors, blurred: PyInstaller's exe manifest
        # doesn't declare DPI awareness. It is per process, so the game isn't affected; customtkinter sets
        # the same mode, and setting it a second time fails without changing anything.
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags | MB_SETFOREGROUND)
    except (AttributeError, OSError):
        pass


def ms_since_process_start() -> float | None:
    """Wall-clock ms since this process was created (with PyInstaller's bootloader); None if unknown"""
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return None
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
    kernel32.GetProcessTimes(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(created),
                             ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
    now = ctypes.c_ulonglong()
    kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
    return (now.value - created.value) / 10_000     # FILETIME ticks are 100 ns
