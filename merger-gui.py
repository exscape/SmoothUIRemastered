"""Smooth UI Merger GUI: phase-0 demo.

Frozen as SmoothUIMerger.exe (windowed, no console). The demo proves that
customtkinter starts from the frozen build, measures how long that takes, and
rehearses what the real tool will do to the mods folder (matters under MO2's VFS):
read other mods' files, and write/read back/delete our own merged package.

Its command line shows at the top. On its first run it saves its settings (so the launcher stops opening
it for that). With --stale (the launcher opened it), it only offers its exit codes, to test the launcher.
"""

import multiprocessing
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from smoothui.launcher import ABORT, PLAY, UPDATING
from smoothui.merger.game import (
    GAME_PATH_ENV,
    MODS_SETTINGS_ENV,
    Settings,
    find_game,
    load_settings,
    save_settings,
)
from smoothui.windows import ms_since_process_start

MODS_DIR = Path(r"D:\Games\The Witcher 3 Remastered\mods")
PACKAGE_NAME = "mod0000_SmoothUI_MergedPatches"
PACKAGE_ROOT = MODS_DIR / PACKAGE_NAME
PACKAGE_DIR = PACKAGE_ROOT / "content"
PACKAGE_FILES = ("blob0.bundle", "metadata.store")
BLOB_PADDING_BYTES = 1 << 20  # random, so a stale read-back can never look identical


def package_listing():
    """Names of the files this process can currently see in PACKAGE_DIR."""
    try:
        return ", ".join(sorted(p.name for p in PACKAGE_DIR.iterdir())) or "(none)"
    except OSError as e:
        return f"(cannot list: {type(e).__name__}: {e})"


def folder_listing():
    """Every folder under MODS_DIR as this process sees it, one relative path per line."""
    errors = []
    folders = [str(x) for x in list(MODS_DIR.glob("*"))]
    lines = [f"Folders under {MODS_DIR} as this process sees them ({len(folders)}):", *folders]
    if errors:
        lines.append(f"{len(errors)} errors while listing, first: {errors[0]}")
    return "\n".join(lines)


def first_run():
    """Save the settings if there are none (or they can't be read); returns what to show about them"""
    try:
        settings = load_settings()
    except Exception as e:  # noqa: BLE001 (written again)
        settings, problem = None, f" (they couldn't be read: {e})"
    else:
        problem = ""
    if settings is not None:
        return f"Settings: game folder {settings.game_path}"
    # find_game(), not game_path(): that would save a test folder from SMOOTHUI_GAME_PATH
    save_settings(Settings(find_game()))
    return f"First run{problem}: settings saved (game folder {find_game()})"


def command_line_text():
    lines = [f"Command line: {subprocess.list2cmdline(sys.argv[1:]) or '(no arguments)'}"]
    for name in (GAME_PATH_ENV, MODS_SETTINGS_ENV):
        if os.environ.get(name):
            lines.append(f"{name}={os.environ[name]}")
    return "\n".join(lines)


class MergerApp(ctk.CTk):
    def __init__(self, stale):
        super().__init__()
        self.title("Smooth UI Merger (demo)")
        self.geometry("820x820")
        self.clicks = 0
        self.exit_code = 0

        ctk.CTkLabel(self, text=command_line_text(), justify="left", font=ctk.CTkFont(size=16, weight="bold"),
                     wraplength=780).pack(padx=20, pady=(16, 0), anchor="w")
        ctk.CTkLabel(self, text="Smooth UI Merger", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(12, 4))
        ctk.CTkLabel(self, text="Phase-0 demo: nothing is really patched yet.").pack()

        self.info = ctk.CTkLabel(self, text="", justify="left")
        self.info.pack(padx=20, pady=16, anchor="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack()
        if stale:
            # Test buttons for the launcher, until stale mode is built. Closing the window exits with 0: play.
            ctk.CTkLabel(self, text="Opened by the launcher: tell it what to do").pack(before=buttons, pady=(0, 6))
            for text, code in (("Play", PLAY), ("Abort game launch", ABORT), ("Updating (don't play)", UPDATING),
                               ("Crash", 1)):
                ctk.CTkButton(buttons, text=f"{text}: {code}", command=lambda c=code: self.exit(c)).pack(side="left",
                                                                                                       padx=6)
        else:
            for text, action in (
                ("Scan existing mods (read only)", self.scan_mods),
                ("Write merged package", self.write_package),
                ("Delete package", self.delete_package),
            ):
                ctk.CTkButton(buttons, text=text, command=lambda a=action: self.run(a)).pack(side="left", padx=6)

        self.result = ctk.CTkLabel(self, text="", justify="left", wraplength=780)
        self.result.pack(padx=20, pady=12, anchor="w")

        if not stale:
            ctk.CTkButton(self, text="Close", command=self.destroy).pack(side="bottom", pady=(0, 20))

        self.folders = ctk.CTkTextbox(self, wrap="none", state="disabled")
        self.folders.pack(side="top", padx=20, pady=(0, 12), fill="both", expand=True)

        # Runs once the event loop is up, i.e. when the window is about to show.
        self.after(0, self.show_info)
        self.after(100, self.refresh_folders)  # after the window is up, so it can't delay it

    def exit(self, code):
        self.exit_code = code
        self.destroy()

    def run(self, action):
        """Button handler. Shows a "click received" line first, so a click that does nothing is visible."""
        self.clicks += 1
        header = f"Click #{self.clicks} at {datetime.now():%H:%M:%S.%f}"[:-3]
        self.result.configure(text=f"{header}: working...")
        self.update_idletasks()
        try:
            report = action()
        except Exception as e:  # noqa: BLE001 (a windowed exe has no console, so show everything)
            report = f"Failed: {type(e).__name__}: {e}"
        self.result.configure(text=f"{header}\n{report}")
        self.refresh_folders()

    def refresh_folders(self):
        try:
            text = folder_listing()
        except Exception as e:  # noqa: BLE001 (same reason as in run())
            text = f"Cannot list folders: {type(e).__name__}: {e}"
        self.folders.configure(state="normal")
        self.folders.delete("1.0", "end")
        self.folders.insert("1.0", text)
        self.folders.configure(state="disabled")

    def scan_mods(self):
        """Walk MODS_DIR and read every .bundle completely. Strictly read-only.

        The real tool does exactly this to find the UI files other mods ship. It also shows
        whether a walk sees files created through the VFS (our own package, below).
        """
        errors = []
        mod_folders = None
        folders = files = bundle_count = bundle_bytes = 0
        ours = []
        start = time.perf_counter()
        for root, dirs, names in os.walk(MODS_DIR, onerror=errors.append):
            if mod_folders is None:
                mod_folders = sorted(dirs)
            folders += len(dirs)
            files += len(names)
            if PACKAGE_NAME in Path(root).parts:
                ours += [str(Path(root, n).relative_to(MODS_DIR)) for n in names]
            for name in names:
                if name.lower().endswith(".bundle"):
                    try:
                        bundle_bytes += len(Path(root, name).read_bytes())
                        bundle_count += 1
                    except OSError as e:
                        errors.append(e)
        ms = (time.perf_counter() - start) * 1000
        shown = mod_folders or []
        lines = [
            f"{MODS_DIR}: {len(shown)} top-level folders, {folders} folders and {files} files in total",
            f"read {bundle_count} .bundle files ({bundle_bytes:,} bytes) in {ms:.0f} ms",
            f"top-level: {', '.join(shown[:20])}{' ...' if len(shown) > 20 else ''}",
            f"our package seen by the walk: {', '.join(ours) if ours else 'no'}",
        ]
        if errors:
            lines.append(f"{len(errors)} errors, first: {errors[0]}")
        return "\n".join(lines)

    def write_package(self):
        """Phase-0 MO2 test: write the real package files, overwriting them every time.

        Creates the folders first if missing (the first build must), then writes each file,
        reads it back by opening it, and only then asks exists() (which can lie under the VFS).
        """
        lines = [f"pid {os.getpid()}"]
        # Always try mkdir and read its answer: Path.mkdir(exist_ok=True) and os.makedirs
        # ask is_dir() when mkdir says "exists", and under MO2's VFS that can disagree.
        for folder in (PACKAGE_DIR.parent, PACKAGE_DIR):  # package folder, then content
            try:
                os.mkdir(folder)
                lines.append(f"mkdir {folder.name}: created")
            except FileExistsError:
                lines.append(f"mkdir {folder.name}: says it already exists")
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"
        payloads = {
            "blob0.bundle": f"blob0 {stamp}\n".encode() + os.urandom(BLOB_PADDING_BYTES),
            "metadata.store": f"metadata {stamp}\n".encode(),
        }
        for name, data in payloads.items():
            path = PACKAGE_DIR / name
            with path.open("wb") as f:
                f.write(data)
            with path.open("rb") as f:
                back = f.read()
            verdict = "identical" if back == data else "DIFFERENT"
            lines.append(f"{name}: wrote {len(data):,} B, read back {len(back):,} B ({verdict}); exists(): {path.exists()}")
        lines.append(f"this process now sees in content: {package_listing()}")
        return "\n".join(lines)

    # TODO: rework once the base functionality is proven: extremely verbose due to debugging
    def delete_package(self):
        """Delete the files this tool writes, then every empty folder inside our package folder.

        Never touches anything outside PACKAGE_ROOT, and never deletes a file we didn't write:
        a folder that still holds one is kept (and so are its parents).
        """
        lines = [f"pid {os.getpid()}"]
        for name in PACKAGE_FILES:
            path = PACKAGE_DIR / name
            try:
                path.unlink()
                lines.append(f"{name}: deleted; exists() afterwards: {path.exists()}")
            except FileNotFoundError:
                lines.append(f"{name}: not found (exists(): {path.exists()})")

        # Bottom-up, so a folder emptied by removing its children is removed too.
        # rmdir only ever removes an empty folder; the listdir check is for the report.
        errors = []
        for root, _, _ in os.walk(PACKAGE_ROOT, topdown=False, onerror=errors.append):
            folder = Path(root)
            left = os.listdir(folder)
            rel = folder.relative_to(MODS_DIR)
            if left:
                lines.append(f"kept {rel}: still holds {len(left)} item(s), e.g. {left[0]}")
                continue
            try:
                os.rmdir(folder)
                lines.append(f"removed empty folder {rel}")
            except OSError as e:
                lines.append(f"could not remove {rel}: {type(e).__name__}: {e}")
        if errors:
            lines.append(f"{len(errors)} errors while walking, first: {errors[0]}")
        lines.append(f"package folder exists(): {PACKAGE_ROOT.exists()}")
        return "\n".join(lines)

    def show_info(self):
        try:
            settings = first_run()
        except Exception as e:  # noqa: BLE001 (shown)
            settings = f"Settings can't be saved: {e}"
        self.info.configure(text="\n".join([
            settings,
            f"Window ready {ms_since_process_start() or 0:.0f} ms after process start",
            f"Frozen: {getattr(sys, 'frozen', False)}",
            f"Python {platform.python_version()}, customtkinter {ctk.__version__}",
            f"Executable: {sys.executable}",
        ]))


def main():
    ctk.set_appearance_mode("system")
    app = MergerApp(stale="--stale" in sys.argv[1:])
    app.mainloop()
    sys.exit(app.exit_code)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # must run first in every frozen main (process pool later)
    main()
