# -*- mode: python ; coding: utf-8 -*-
# Build from the repo root, on Windows Python (env_pyi), never under WSL:
#     env_pyi/Scripts/pyinstaller.exe --clean --noconfirm smoothui-merger.spec
# Output: dist/SmoothUIMerger/ with SmoothUILauncher.exe, SmoothUIMerger.exe, _internal/
import shutil
from fnmatch import fnmatch
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

APP_DIR_NAME = "SmoothUIMerger"  # the folder in dist/
TOOL_VERSION = (0, 1, 0, 0)
COMPANY = "Aeluwas"
PRODUCT = "Smooth UI Remastered"
ICON = Path(SPECPATH) / "smoothui.ico"  # optional: used only if the file exists


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
