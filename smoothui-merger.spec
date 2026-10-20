# -*- mode: python ; coding: utf-8 -*-
# Build from the repo root, on 64-bit Windows Python (env_gui, see requirements_gui.txt), never under WSL:
#     env_gui/Scripts/pyinstaller.exe --clean --noconfirm smoothui-merger.spec
# Output: dist/SmoothUIMerger/ with SmoothUILauncher.exe, SmoothUIMerger.exe, _internal/
import hashlib
import shutil
from fnmatch import fnmatch
from pathlib import Path

from PyInstaller import HOMEPATH, PLATFORM
from PyInstaller import __version__ as PYINSTALLER_VERSION
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

APP_DIR_NAME = "SmoothUIMerger"  # the folder in dist/
TOOL_VERSION = (0, 1, 0, 0)
COMPANY = "Aeluwas"
PRODUCT = "Smooth UI Remastered"
ICON = Path(SPECPATH) / "smoothui.ico"  # optional: used only if the file exists

# SHA-256 of the bootloaders that come prebuilt with each PyInstaller version (the same in its wheel and its
# sdist). A release must use one compiled here; dev-tools/pyinstaller-hashes.py prints a new version's entry.
PREBUILT_BOOTLOADERS = {
    "6.22.3": {
        "run.exe": "581ea23eb35cee8f9df8835c892e91416df95ca629b1a6050289aea19390e08d",
        "run_d.exe": "61569642ef07b9f3c730eb6f2ab21350e74062ff409ef200bad63d038c510387",
        "runw.exe": "2291f269c3a3804fde1079462239e09a8b32fffbeeaa8f628a531faf5be77d41",
        "runw_d.exe": "d85a1689b986222f6950f3ba878d791068725d789c1a8e4a783a520f788c4560",
    },
}


def check_bootloader():
    """Refuse to build with a prebuilt bootloader, e.g. when the pyinstaller.exe in PATH runs rather than
    env_gui's, or when env_gui's PyInstaller was installed without compiling one"""
    if PLATFORM != "Windows-64bit-intel":
        raise SystemExit(f"ERROR: build on 64-bit Windows Python, not {PLATFORM}")
    prebuilt = PREBUILT_BOOTLOADERS.get(PYINSTALLER_VERSION)
    if prebuilt is None:
        raise SystemExit(f"ERROR: can't tell whether PyInstaller {PYINSTALLER_VERSION}'s bootloader was compiled "
                         f"here: add its prebuilt ones to PREBUILT_BOOTLOADERS (python dev-tools/pyinstaller-hashes.py "
                         f"{PYINSTALLER_VERSION} prints them)")
    folder = Path(HOMEPATH) / "PyInstaller" / "bootloader" / PLATFORM
    found = [name for name, sha256 in prebuilt.items()
             if (folder / name).is_file() and hashlib.sha256((folder / name).read_bytes()).hexdigest() == sha256]
    if found:
        raise SystemExit(f"ERROR: {', '.join(found)} in {folder} came prebuilt with PyInstaller, not compiled here. "
                         "Use env_gui's pyinstaller.exe, or reinstall PyInstaller as requirements_gui.txt says.")
    print("Bootloader hash verified: self-built bootloader is being used")


check_bootloader()


def version_info(file_description, original_filename):
    dotted = ".".join(map(str, TOOL_VERSION))
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=TOOL_VERSION, prodvers=TOOL_VERSION),
        kids=[
            StringFileInfo([StringTable("040904B0", [  # US English, Unicode
                StringStruct("CompanyName", COMPANY),
                StringStruct("FileDescription", file_description),
                StringStruct("FileVersion", dotted),
                StringStruct("InternalName", Path(original_filename).stem),
                StringStruct("LegalCopyright", f"Copyright (c) {COMPANY}"),
                StringStruct("OriginalFilename", original_filename),
                StringStruct("ProductName", PRODUCT),
                StringStruct("ProductVersion", dotted),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


# One Analysis per exe, so the launcher's embedded PYZ holds only what it imports.
# Do NOT use MERGE(): it turns every exe into a onefile-style exe that extracts to %TEMP%.
launcher_a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["tkinter", "customtkinter", "_tkinter"],  # keep the launcher light
    noarchive=False,
    optimize=0,  # keep asserts
)
merger_a = Analysis(
    ["merger-gui.py"],
    pathex=[],
    binaries=[],
    datas=collect_data_files("customtkinter"),  # themes/fonts; it fails at startup without these
    hiddenimports=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

launcher_exe = EXE(
    PYZ(launcher_a.pure),
    launcher_a.scripts,
    [],
    exclude_binaries=True,  # onedir
    name="SmoothUILauncher",
    console=False,
    upx=False,
    icon=str(ICON) if ICON.exists() else None,
    version=version_info("Smooth UI Remastered launcher", "SmoothUILauncher.exe"),
)
merger_exe = EXE(
    PYZ(merger_a.pure),
    merger_a.scripts,
    [],
    exclude_binaries=True,
    name="SmoothUIMerger",
    console=False,
    upx=False,
    icon=str(ICON) if ICON.exists() else None,
    version=version_info("Smooth UI Remastered merger", "SmoothUIMerger.exe"),
)

# Both exes share one _internal\ ; COLLECT drops duplicate entries (same destination name).
coll = COLLECT(
    launcher_exe, launcher_a.binaries, launcher_a.datas,
    merger_exe, merger_a.binaries, merger_a.datas,
    strip=False,
    upx=False,
    name=APP_DIR_NAME,
)

# Our own files go next to the exes, not into _internal\ (which is PyInstaller's; COLLECT
# also refuses datas destinations outside it). COLLECT has finished writing by this point
# and wipes the whole folder at the start of every build, so this must run on every build.
# copyfile, not copy2: no metadata, which fails on some drives.
out_dir = Path(DISTPATH) / APP_DIR_NAME
manifests = sorted(
    p for p in (Path(SPECPATH) / "manifests").glob("*.json")
    if not fnmatch(p.name, "*dev*")  # same as .gitignore; manifests/compat/ is never matched
)
if not manifests:
    raise SystemExit("ERROR: no release manifests found in manifests/")
(out_dir / "manifests").mkdir()
for p in manifests:
    shutil.copyfile(p, out_dir / "manifests" / p.name)
