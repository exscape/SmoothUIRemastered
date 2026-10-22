"""SmoothUILauncher.exe (windowed): starts the game, and first the merger if its package is stale. See
smoothui/launcher.py.

    SmoothUILauncher.exe "C:\\...\\witcher3.exe"   (Steam's %command%: the whole game command)
    SmoothUILauncher.exe                           (the game's arguments, for its exe in the game folder)
"""

from smoothui.launcher import run

if __name__ == "__main__":
    run()
