"""What both exes need from Windows itself: message boxes, when the process started, and (the merger) a
window on top, one merger at a time, whether a DLL is loaded, and waiting for a process. Light (ctypes only);
elsewhere (tests under WSL), the boxes show nothing, the start time is unknown, each process is the only one,
no DLL is loaded, and every process has ended."""

import ctypes

TITLE = "Smooth UI Remastered"

MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000

PROCESS_PER_MONITOR_DPI_AWARE = 2
ERROR_ALREADY_EXISTS = 183

# The named mutexes this process holds (see only_instance): kept until it ends
_mutexes = []


def msg_box(text: str, flags: int = MB_ICONWARNING):
    """Show a message box and wait for the user to close it. Never raises."""
    try:
        # Without this, Windows scales the box up on HiDPI monitors, blurred: PyInstaller's exe manifest
        # doesn't declare DPI awareness. It is per process, so the game isn't affected; Qt sets its own
        # (newer) mode, and setting one a second time fails without changing anything.
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


def only_instance(name: str) -> bool:
    """Whether no other process holds the named mutex name. This one holds it from now on, until it ends
    (Windows then releases it, however it ends). True if Windows can't tell, and outside Windows."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return True
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True
    _mutexes.append(handle)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def release_mutexes():
    """Stop holding the named mutexes this process holds (see only_instance), before it ends"""
    try:
        kernel32 = ctypes.WinDLL("kernel32")
    except (AttributeError, OSError):
        return
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    while _mutexes:
        kernel32.CloseHandle(_mutexes.pop())


def find_window(title: str, class_prefix: str) -> int | None:
    """The handle of a top-level window (shown or not) with this title, whose window class starts with
    class_prefix (so that, say, an Explorer window on a folder of the same name doesn't count)"""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return None
    user32.FindWindowExW.restype = ctypes.c_void_p
    user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    hwnd = None
    while hwnd := user32.FindWindowExW(None, hwnd, None, title):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, len(name))
        if name.value.startswith(class_prefix):
            return hwnd
    return None


def bring_to_front(hwnd: int):
    """Restore a window if it is minimized, and bring it to the front. Windows may refuse it the focus (when
    this process isn't in the foreground itself): put on top of the others for a moment, it shows anyway."""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return
    sw_restore = 9
    if user32.IsIconic(ctypes.c_void_p(hwnd)):
        user32.ShowWindow(ctypes.c_void_p(hwnd), sw_restore)
    set_topmost(hwnd, True)
    set_topmost(hwnd, False)    # (it stays above the windows it was put above)
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))


def set_topmost(hwnd: int, on: bool):
    """Keep a window above all others, or not any more (SetWindowPos; Qt's flag would make the window anew)"""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return
    hwnd_topmost, hwnd_notopmost = -1, -2
    swp_nosize, swp_nomove, swp_noactivate = 0x1, 0x2, 0x10
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos(ctypes.c_void_p(hwnd), hwnd_topmost if on else hwnd_notopmost, 0, 0, 0, 0,
                        swp_nosize | swp_nomove | swp_noactivate)


def module_loaded(name: str) -> bool:
    """Whether a DLL of this name is loaded into this process (e.g. a mod manager's, injected); False
    outside Windows"""
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return False
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    return bool(kernel32.GetModuleHandleW(name))


def wait_for_process(pid: int, seconds: float) -> bool:
    """Wait until the process with this ID has ended; True if it has (also if there is no such process),
    False if it still runs after the time. True outside Windows."""
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return True
    synchronize, wait_object_0 = 0x100000, 0
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True     # gone (or not ours to wait for)
    try:
        return kernel32.WaitForSingleObject(handle, round(seconds * 1000)) == wait_object_0
    finally:
        kernel32.CloseHandle(handle)
