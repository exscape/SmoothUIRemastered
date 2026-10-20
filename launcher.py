"""Smooth UI launcher: phase-0 demo (see docs/plan-local-patcher.md).

Frozen as SmoothUILauncher.exe (windowed, no console). Keep the imports minimal
(stdlib only, no Tk): this exe must start in well under a second. The real
fingerprint check comes in phase 3; for now this only shows the message box and
passes the command line through to the game, the way Vortex/MO2/Steam will use it.

    SmoothUILauncher.exe "C:\\...\\witcher3.exe" -windowed   (args are the game's command)
    SmoothUILauncher.exe                                     (no game: dialog only)
"""

import ctypes
import os
import subprocess
import sys

MB_YESNOCANCEL = 0x03
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10
MB_SETFOREGROUND = 0x10000
IDYES, IDNO = 6, 7

MERGER_EXE = "SmoothUIMerger.exe"


def msg_box(text, title="Smooth UI Remastered", flags=0):
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags | MB_SETFOREGROUND)


def ms_since_process_start():
    """Wall-clock ms since this process was created (includes the bootloader)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
    kernel32.GetProcessTimes(
        ctypes.c_void_p(kernel32.GetCurrentProcess()),
        ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
    )
    now = ctypes.c_ulonglong()
    kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))
    return (now.value - created.value) / 10_000  # FILETIME ticks are 100 ns


def main():
    game_command = sys.argv[1:]
    elapsed = ms_since_process_start()

    answer = msg_box(
        "DEMO: pretend the Smooth UI patches are out of date.\n"
        f"(Process start to this dialog: {elapsed:.0f} ms)\n\n"
        "Yes = Rebuild (opens the Merger)\n"
        "No = Launch anyway\n"
        "Cancel = Don't start the game",
        flags=MB_YESNOCANCEL | MB_ICONWARNING,
    )

    if answer == IDYES:
        merger = os.path.join(os.path.dirname(sys.executable), MERGER_EXE)
        if os.path.exists(merger):
            subprocess.Popen([merger])
        else:
            msg_box(f"{MERGER_EXE} was not found next to the launcher:\n{merger}", flags=MB_ICONERROR)
        return 0

    if answer == IDNO:
        if not game_command:
            msg_box("No game command was passed, so there is nothing to launch.")
            return 0
        # Wait for the game: Steam and MO2 track the process they started.
        return subprocess.run(game_command, check=False).returncode

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 (top-level handler)
        # The real launcher must never stop people playing: warn, then launch anyway.
        # (Demo: just report it.)
        msg_box(f"{type(e).__name__}: {e}", flags=MB_ICONERROR)
        sys.exit(1)
