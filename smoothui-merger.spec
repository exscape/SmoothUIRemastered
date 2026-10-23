# -*- mode: python ; coding: utf-8 -*-
# Build from the repo root, on 64-bit Windows Python (env_gui, see requirements_gui.txt), never under WSL's
# Python (build-tools.sh runs this from WSL):
#     env_gui/Scripts/pyinstaller.exe --clean --noconfirm smoothui-merger.spec
#     env_gui/Scripts/pyinstaller.exe --clean --noconfirm smoothui-merger.spec -- --dev-manifests
# --dev-manifests: also ship the manifests of Smooth UI's dev builds, so a test build knows an installed dev
# build. Never for a release.
# Output: dist/SmoothUIMerger/ with SmoothUILauncher.exe, SmoothUIMerger.exe, _internal/
import argparse
import hashlib
import importlib.metadata
import platform
import re
import shutil
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path, PurePath

from PyInstaller import HOMEPATH, PLATFORM
from PyInstaller import __version__ as PYINSTALLER_VERSION
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

APP_DIR_NAME = "SmoothUIMerger"  # the folder in dist/
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

# PyInstaller passes the arguments after -- in sys.argv
spec_parser = argparse.ArgumentParser(prog="smoothui-merger.spec")
spec_parser.add_argument("--dev-manifests", action="store_true",
                         help="also ship the dev builds' manifests (for test builds, never for a release)")
SPEC_ARGS = spec_parser.parse_args(sys.argv[1:])


def read_merger_version():
    """[versions] merger_version in config/config.toml: X.Y.Z (see smoothui/merger/__init__.py)"""
    path = Path(SPECPATH) / "config" / "config.toml"
    with open(path, "rb") as f:
        version = tomllib.load(f).get("versions", {}).get("merger_version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"ERROR: [versions] merger_version in {path} must be X.Y.Z, not {version!r}")
    return version


# The version of the exes, and of the merger, which reads it from the file written here (in _internal\, at
# the path of smoothui/merger/, whose VERSION_FILE it is): a frozen tool has no config.toml
MERGER_VERSION = read_merger_version()
TOOL_VERSION = (*map(int, MERGER_VERSION.split(".")), 0)
merger_version_file = Path(workpath) / "merger_version.txt"
merger_version_file.parent.mkdir(parents=True, exist_ok=True)
merger_version_file.write_text(MERGER_VERSION + "\n", encoding="utf-8")
print(f"Merger version {MERGER_VERSION}")


def is_lgpl(dist):
    meta = dist.metadata
    text = " ".join([meta.get("License-Expression") or "", meta.get("License") or "", *(meta.get_all("Classifier") or [])])
    return "LGPL" in text or "Lesser General Public" in text


# The LGPL lets users replace the library with a modified version: collect LGPL packages (py7zr and
# its compression libraries) as .py files in _internal\ rather than into the exe's archive. (Their
# compiled extensions are separate files anyway.)
LGPL_MODULES = sorted(module for module, dists in importlib.metadata.packages_distributions().items()
                      if any(is_lgpl(importlib.metadata.distribution(name)) for name in dists))


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
    # Keep the launcher light. PyInstaller follows every import, also those inside functions and under
    # TYPE_CHECKING; the launch check needs none of these (tests/test_state.py LightImports)
    excludes=["tkinter", "_tkinter", "PySide6", "shiboken6", "smoothui.patcher", "smoothui.build", "smoothui.log",
              "smoothui.merger.policy", "smoothui.merger.builder", "smoothui.merger.gui", "smoothui.merger.window",
              "colorama", "lz4"],
    noarchive=False,
    optimize=0,  # keep asserts
)
# Files a module reads next to itself (Path(__file__)), at its path in _internal\: the fixes' compiled
# ActionScript (gwent_speed.abc), and the merger window's theme and fonts. grep -rn __file__ smoothui finds the
# code that does it.
module_files = [(str(p), p.parent.relative_to(SPECPATH).as_posix()) for p in sorted(Path(SPECPATH, "smoothui").rglob("*.abc"))]
if not module_files:
    raise SystemExit("ERROR: no .abc files found in smoothui/")
module_files.append((str(Path(SPECPATH, "smoothui", "merger", "theme.json")), "smoothui/merger"))
module_files += [(str(p), "smoothui/merger/fonts") for p in sorted(Path(SPECPATH, "smoothui", "merger", "fonts").glob("*.ttf"))]
merger_a = Analysis(
    ["merger-gui.py"],
    pathex=[],
    binaries=[],
    # The merger's version, and the module files
    datas=[(str(merger_version_file), "smoothui/merger"), *module_files],
    hiddenimports=[],
    # pybcj, pyppmd and inflate64 (for py7zr) fall back to importing importlib_metadata on Python < 3.8;
    # PyInstaller resolves that to setuptools' vendored copy, which pulls in all of setuptools.
    # (Excluding importlib_metadata itself makes PyInstaller's setuptools hook fail.)
    excludes=["setuptools", "_distutils_hack", "pkg_resources", "tkinter", "_tkinter"],
    noarchive=False,
    optimize=0,
    module_collection_mode={module: "py" for module in LGPL_MODULES},
)


def qt_file_needed(dest):
    """Qt (PySide6): only what a Widgets window loads. Not the software OpenGL fallback (20 MB), Qt
    WebEngine's ICU data, the translations, the Qt modules the merger doesn't use, nor plugins other than the
    Windows platform, the styles and the .ico reader (for the window's icon)."""
    path = PurePath(dest)
    if path.parts[0] != "PySide6":
        return True
    if path.name in QT_DROPPED_FILES or "translations" in path.parts:
        return False
    if "plugins" in path.parts:
        plugin = "/".join(path.parts[path.parts.index("plugins") + 1:])
        return plugin in ("platforms/qwindows.dll", "imageformats/qico.dll") or plugin.startswith("styles/")
    return True


QT_DROPPED_FILES = {"opengl32sw.dll", "icudtl.dat", "QtNetwork.pyd", "Qt6Svg.dll", "Qt6Pdf.dll", "Qt6Network.dll",
                    "Qt6OpenGL.dll", "Qt6Qml.dll", "Qt6Quick.dll", "Qt6VirtualKeyboard.dll", "Qt6QmlModels.dll",
                    "Qt6QmlMeta.dll", "Qt6QmlWorkerScript.dll"}
merger_a.binaries = [entry for entry in merger_a.binaries if qt_file_needed(entry[0])]
merger_a.datas = [entry for entry in merger_a.datas if qt_file_needed(entry[0])]


def is_ucrt(dest_name):
    name = dest_name.lower()
    return name == "ucrtbase.dll" or name.startswith("api-ms-win-")


# The Universal CRT is part of Windows 10 and later, so it's never shipped. (PyInstaller collects any copy it
# finds in PATH, e.g. a JDK's.) VCRUNTIME140.dll is not part of Windows, and stays.
for a in (launcher_a, merger_a):
    a.binaries = [entry for entry in a.binaries if not is_ucrt(entry[0])]

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
    if (SPEC_ARGS.dev_manifests or not fnmatch(p.name, "*dev*")) and not p.name == "v0.2.json"
)
if not manifests:
    raise SystemExit("ERROR: no release manifests found in manifests/")
dev_manifests = [p.name for p in manifests if fnmatch(p.name, "*dev*")]
if dev_manifests:
    print(f"Dev manifests included (a test build, not for a release): {', '.join(dev_manifests)}")
(out_dir / "manifests").mkdir()
for p in manifests:
    shutil.copyfile(p, out_dir / "manifests" / p.name)


# Licenses: ours next to the exes; in licenses\, Python's (which also covers what it includes: OpenSSL,
# bzip2, libffi, the Microsoft runtime), PyInstaller's (its bootloader is in each exe), those of each package
# with files in the build, from its .dist-info folder, and the window's font's.
LICENSE_FILE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
# PySide6's and shiboken6's .dist-info hold only Qt's commercial license text: their LGPL texts, where their
# source code is, and the notices of the third-party code in Qt are in assets/licenses/PySide6/ instead (written by
# dev-tools/qt-licenses.py, its README by hand)
QT_DISTRIBUTIONS = {"PySide6_Essentials", "shiboken6"}
QT_LICENSES = Path(SPECPATH) / "assets" / "licenses" / "PySide6"
FONT_LICENSE = Path(SPECPATH) / "smoothui" / "merger" / "fonts" / "OFL.txt"


def bundled_distributions(*analyses):
    """The installed distributions (packages) that have modules or files in the build"""
    top_level = {re.split(r"[./\\]", entry[0])[0] for a in analyses for entry in a.pure + a.binaries + a.datas}
    providers = importlib.metadata.packages_distributions()
    names = {name for module in top_level for name in providers.get(module, [])}
    return sorted((importlib.metadata.distribution(name) for name in names), key=lambda d: d.metadata["Name"].lower())


def license_files(dist):
    """The license files in a distribution's .dist-info folder (or its licenses\\ subfolder)"""
    return [f for f in dist.files or []
            if f.parts[0].endswith(".dist-info") and f.name.upper().startswith(LICENSE_FILE_PREFIXES)]


shutil.copyfile(Path(SPECPATH) / "LICENSE.txt", out_dir / "LICENSE.txt")
licenses_dir = out_dir / "licenses"
python_version = platform.python_version()
(licenses_dir / "Python").mkdir(parents=True)
shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses_dir / "Python" / "LICENSE.txt")
readme = [
    f"Third-party software in {PRODUCT}'s tools; each one's license is in the folder of the same name.",
    "",
    f"Python {python_version}, with the libraries it includes (OpenSSL, bzip2, libffi): https://www.python.org/",
]
lgpl = []
for dist in [importlib.metadata.distribution("pyinstaller"), *bundled_distributions(launcher_a, merger_a)]:
    name, version = dist.metadata["Name"], dist.version
    note = " (its bootloader, in each exe)" if name.lower() == "pyinstaller" else ""
    if name in QT_DISTRIBUTIONS:
        (licenses_dir / "PySide6").mkdir(exist_ok=True)
        for f in sorted(QT_LICENSES.iterdir()):
            shutil.copyfile(f, licenses_dir / "PySide6" / f.name)
        readme.append(f"{name} {version} (LGPL; see PySide6\\README.txt): https://pypi.org/project/{name}/{version}/")
        continue
    files = license_files(dist)
    if not files:
        raise SystemExit(f"ERROR: {name} {version} is in the build but has no license file in its .dist-info")
    (licenses_dir / name).mkdir()
    for f in files:
        shutil.copyfile(dist.locate_file(f), licenses_dir / name / f.name)
    readme.append(f"{name} {version}{note}: https://pypi.org/project/{name}/{version}/")
    if is_lgpl(dist):
        lgpl.append(name)
(licenses_dir / "Roboto").mkdir()
shutil.copyfile(FONT_LICENSE, licenses_dir / "Roboto" / FONT_LICENSE.name)
readme.append("Roboto 3.016 (the merger window's font): https://github.com/googlefonts/roboto-3-classic")
if lgpl:
    readme += ["", (f"{', '.join(lgpl)} (LGPL) are in _internal\\ as .py files, so they can be replaced by modified "
                    "versions. Their source code is at the links above.")]
(licenses_dir / "README.txt").write_text("\n".join(readme) + "\n", encoding="utf-8", newline="\r\n")
