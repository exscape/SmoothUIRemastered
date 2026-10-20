"""Smooth UI Merger GUI: phase-0 demo (see docs/plan-local-patcher.md).

Frozen as SmoothUIMerger.exe (windowed, no console). The demo proves that
customtkinter starts from the frozen build, measures how long that takes, and
rehearses what the real tool will do to the mods folder (matters under MO2's VFS):
read other mods' files, and write/read back/delete our own merged package.
"""

import ctypes
import multiprocessing
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

MODS_DIR = Path(r"D:\Games\The Witcher 3 Remastered\mods")
PACKAGE_NAME = "mod0000_SmoothUI_MergedPatches"
PACKAGE_ROOT = MODS_DIR / PACKAGE_NAME
PACKAGE_DIR = PACKAGE_ROOT / "content"
PACKAGE_FILES = ("blob0.bundle", "metadata.store")
BLOB_PADDING_BYTES = 1 << 20  # random, so a stale read-back can never look identical


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


class MergerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Smooth UI Merger (demo)")
        self.geometry("820x820")
        self.clicks = 0

        ctk.CTkLabel(self, text="Smooth UI Merger", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(20, 4))
        ctk.CTkLabel(self, text="Phase-0 demo: nothing is really patched yet.").pack()

        self.info = ctk.CTkLabel(self, text="", justify="left")
        self.info.pack(padx=20, pady=16, anchor="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack()
        for text, action in (
            ("Scan existing mods (read only)", self.scan_mods),
            ("Write merged package", self.write_package),
            ("Delete package", self.delete_package),
        ):
            ctk.CTkButton(buttons, text=text, command=lambda a=action: self.run(a)).pack(side="left", padx=6)

        self.result = ctk.CTkLabel(self, text="", justify="left", wraplength=780)
        self.result.pack(padx=20, pady=12, anchor="w")

        ctk.CTkButton(self, text="Close", command=self.destroy).pack(side="bottom", pady=(0, 20))

        self.folders = ctk.CTkTextbox(self, wrap="none", state="disabled")
        self.folders.pack(side="top", padx=20, pady=(0, 12), fill="both", expand=True)

        # Runs once the event loop is up, i.e. when the window is about to show.
        self.after(0, self.show_info)
        self.after(100, self.refresh_folders)  # after the window is up, so it can't delay it

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
        self.info.configure(text="\n".join([
            f"Window ready {ms_since_process_start():.0f} ms after process start",
            f"Frozen: {getattr(sys, 'frozen', False)}",
            f"Python {platform.python_version()}, customtkinter {ctk.__version__}",
            f"Executable: {sys.executable}",
        ]))


def main():
    ctk.set_appearance_mode("system")
    MergerApp().mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()  # must run first in every frozen main (process pool later)
    main()
