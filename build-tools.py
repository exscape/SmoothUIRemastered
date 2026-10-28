#!/usr/bin/env python3
"""Builds the tools' exes with Nuitka: SmoothUILauncher.exe and SmoothUIMerger.exe, in one folder. Builds on
64-bit Windows Python (env_gui, see requirements_gui.txt); from WSL, this runs itself again on env_gui's.
    env_gui/Scripts/python.exe build-tools.py      (or from WSL: ./build-tools.py)
        build into build/dist/SmoothUIMerger/, then mirror that into dist/SmoothUIMerger/, keeping its data/
        (the tools' settings and logs)
    --dev-manifests   a test build that also knows Smooth UI's dev builds (never for a release)
    --version X.Y.Z   a test build that says it is that version (not config.toml's)
    --zip [ZIP]       zip the build as a release ships it (default dist/SmoothUITools-X.Y.Z.zip), instead of
                      mirroring it into dist/SmoothUIMerger/
    --out DIR         build into DIR instead (inside build/), and leave it there

Each exe is built on its own, so the launcher holds only what it imports (no Qt), then both go into the build
folder, which is deleted first. Nuitka puts the files the exes need next to them; the files both builds have
must be the same. Our own files go there too: manifests\\, LICENSE.txt, README_tools.md, and licenses\\.

dist/SmoothUIMerger/ itself is never deleted, only mirrored into: WSL1 can keep showing a deleted folder that
Windows made again (while a WSL process, e.g. a file watcher, still holds the old one).
"""
import argparse
import filecmp
import importlib.metadata
import platform
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from fnmatch import fnmatch
from pathlib import Path, PureWindowsPath
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build" / "dist" / "SmoothUIMerger"
INSTALL_DIR = ROOT / "dist" / "SmoothUIMerger"
ZIP_FOLDER = "Smooth UI Tools"          # the folder at the root of a release zip
WORK_DIR = ROOT / "build" / "nuitka"    # Nuitka's build folders and reports, and the version file
COMPANY = "Aeluwas"
PRODUCT = "Smooth UI Remastered"

# The version file, at the path of smoothui/merger/, whose VERSION_FILE it is: a frozen tool has no config.toml
VERSION_DEST = "smoothui/merger/tools_version.txt"
# The names this version ships at the top of the folder, which an update replaces (self_update.SHIPPED_LIST)
SHIPPED_DEST = "smoothui/merger/shipped.txt"

# Keep the launcher light. Nuitka follows every import, also those inside functions and under TYPE_CHECKING;
# the launch check needs none of these (tests/test_state.py LightImports)
LAUNCHER_EXCLUDES = ["tkinter", "_tkinter", "PySide6", "shiboken6", "smoothui.patcher", "smoothui.build",
                     "smoothui.log", "smoothui.merger.policy", "smoothui.merger.builder", "smoothui.merger.gui",
                     "smoothui.merger.window", "colorama", "lz4"]
# pybcj, pyppmd and inflate64 (for py7zr) fall back to importing importlib_metadata on Python < 3.8, which would
# pull in all of setuptools. PySide6's __init__ imports QtNetwork, which the merger doesn't use.
MERGER_EXCLUDES = ["setuptools", "_distutils_hack", "pkg_resources", "tkinter", "_tkinter", "PySide6.QtNetwork"]

# Qt (PySide6): only what a Widgets window loads. Not the Qt modules the merger doesn't use, nor plugins other
# than the Windows platform, the styles and the .ico reader (for the window's icon). Nuitka's PySide6 plugin
# always adds more plugins, so the others are left out by name: those PySide6 has, below its plugins\.
QT_PLUGINS_KEPT = ("platforms/qwindows.dll", "imageformats/qico.dll", "styles/*")
QT_DROPPED_FILES = ["opengl32sw.dll", "qt6network.dll", "qt6svg.dll", "qt6pdf.dll", "qt6opengl.dll", "qt6qml.dll",
                    "qt6quick.dll", "qt6virtualkeyboard.dll", "qt6qmlmodels.dll", "qt6qmlmeta.dll",
                    "qt6qmlworkerscript.dll"]
QT_PLUGIN_DEST = "PySide6/qt-plugins"   # where Nuitka puts them


def parse_args():
    parser = argparse.ArgumentParser(description="Build the tools' exes with Nuitka")
    parser.add_argument("--dev-manifests", action="store_true",
                        help="also ship the dev builds' manifests (for test builds, never for a release)")
    parser.add_argument("--version", help="X.Y.Z instead of tools_version in config.toml (for test builds)")
    parser.add_argument("--zip", nargs="?", const="", metavar="ZIP",
                        help="zip the build (default dist/SmoothUITools-X.Y.Z-test.zip) instead of mirroring it into "
                             "dist/SmoothUIMerger/")
    parser.add_argument("--out", type=Path, metavar="DIR",
                        help="build into DIR (inside build/, deleted first) and leave it there")
    # zip: None, "" (the default zip) or a path. No type=Path: argparse would also convert the "" to Path("."), the current folder
    return parser.parse_args()


def in_wsl():
    return platform.system() == "Linux" and "microsoft" in platform.release().lower()


def windows_path(path):
    """A WSL path as Windows sees it; it need not exist yet"""
    path = path.resolve()
    existing = next(p for p in (path, *path.parents) if p.exists())
    base = subprocess.run(["wslpath", "-w", str(existing)], capture_output=True, text=True, check=True).stdout.strip()
    return str(PureWindowsPath(base, *path.relative_to(existing).parts))


def run_on_windows(args):
    """From WSL: run this script again on env_gui's Windows Python, with the paths as Windows sees them"""
    python = ROOT / "env_gui" / "Scripts" / "python.exe"
    if not python.is_file():
        raise SystemExit(f"ERROR: no {python.relative_to(ROOT)} (see requirements_gui.txt)")
    if not re.match(r"[A-Za-z]:\\", windows_path(ROOT)):
        raise SystemExit(f"ERROR: the repo must be on a Windows drive, not {windows_path(ROOT)}")
    command = [str(python), windows_path(Path(__file__))]
    if args.dev_manifests:
        command.append("--dev-manifests")
    if args.version is not None:
        command += ["--version", args.version]
    if args.zip is not None:
        command += ["--zip", *([windows_path(Path(args.zip))] if args.zip else [])]
    if args.out is not None:
        command += ["--out", windows_path(args.out)]
    raise SystemExit(subprocess.run(command, cwd=ROOT, check=False).returncode)


def check_python():
    if platform.system() != "Windows" or platform.machine() != "AMD64":
        raise SystemExit(f"ERROR: build on 64-bit Windows Python, not {platform.system()} {platform.machine()}")
    # Nuitka 4.3.1 builds 3.15 exes that fail at run time (its lazy imports)
    if sys.version_info >= (3, 15):
        raise SystemExit(f"ERROR: build with Python 3.14, not {platform.python_version()}")


def read_tools_version(version):
    """[versions] tools_version in config/config.toml: X.Y.Z (see smoothui/merger/__init__.py), or --version"""
    if version is not None:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise SystemExit(f"ERROR: --version must be X.Y.Z, not {version!r}")
        return version
    path = ROOT / "config" / "config.toml"
    with open(path, "rb") as f:
        version = tomllib.load(f).get("versions", {}).get("tools_version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"ERROR: [versions] tools_version in {path} must be X.Y.Z, not {version!r}")
    return version


def qt_dropped():
    """--noinclude-dlls options for the Qt files the merger doesn't need"""
    # Only on env_gui's Python, not WSL's (run_on_windows)
    import PySide6
    plugins = Path(PySide6.__file__).parent / "plugins"
    dropped = [f"{QT_PLUGIN_DEST}/{p.relative_to(plugins).as_posix()}" for p in sorted(plugins.rglob("*.dll"))
               if not any(fnmatch(p.relative_to(plugins).as_posix(), kept) for kept in QT_PLUGINS_KEPT)]
    return [f"--noinclude-dlls={name}" for name in dropped + QT_DROPPED_FILES]


def merger_data_files(version_file):
    """Files a module reads next to itself (Path(__file__)), at its path: the fixes' compiled ActionScript
    (gwent_speed.abc), and the merger window's theme, fonts and icon. grep -rn __file__ smoothui finds the code
    that does it."""
    files = [(p, p.relative_to(ROOT).as_posix()) for p in sorted((ROOT / "smoothui").rglob("*.abc"))]
    if not files:
        raise SystemExit("ERROR: no .abc files found in smoothui/")
    merger = ROOT / "smoothui" / "merger"
    files.append((merger / "theme.json", "smoothui/merger/theme.json"))
    files.append((ROOT / "assets" / "Merger.ico", "smoothui/merger/Merger.ico"))   # the window's icon (window.py ICON_FILE)
    files += [(p, f"smoothui/merger/fonts/{p.name}") for p in sorted((merger / "fonts").glob("*.ttf"))]
    files.append((version_file, VERSION_DEST))
    return [f"--include-data-files={source}={dest}" for source, dest in files]


def nuitka(script, exe_name, description, icon, version, options):
    """Build script into WORK_DIR/<its name>/<its name>.dist, with Nuitka's report on it next to that folder
    (report.xml); returns the folder"""
    out = WORK_DIR / Path(script).stem
    dotted = f"{version}.0"
    command = [
        sys.executable, "-m", "nuitka", "--mode=standalone", "--msvc=latest", "--assume-yes-for-downloads",
        "--windows-console-mode=disable", f"--windows-icon-from-ico={ROOT / 'assets' / icon}",
        f"--company-name={COMPANY}", f"--product-name={PRODUCT}", f"--file-version={dotted}",
        f"--product-version={dotted}", f"--file-description={description}", f"--copyright=Copyright (c) {COMPANY}",
        f"--output-filename={exe_name}", f"--output-dir={out}", f"--report={out / 'report.xml'}",
        *options, str(ROOT / script),
    ]
    print(f"Building {exe_name}", flush=True)
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        raise SystemExit(f"ERROR: Nuitka failed to build {exe_name}")
    return out / f"{Path(script).stem}.dist"


def report_modules(dist):
    """The names of the modules in a build, from Nuitka's report on it"""
    return {module.get("name") for module in ElementTree.parse(dist.parent / "report.xml").getroot().iter("module")}


def check_launcher(dist):
    """The launcher must hold none of LAUNCHER_EXCLUDES (Nuitka leaves out what they import, not an import
    that reaches them some other way): the exe's counterpart of tests/test_state.py LightImports"""
    found = sorted(name for name in report_modules(dist)
                   if any(name == excluded or name.startswith(excluded + ".") for excluded in LAUNCHER_EXCLUDES))
    if found:
        raise SystemExit(f"ERROR: the launcher holds modules it must not: {', '.join(found)}")


def combine(dists, out_dir):
    """Copy the builds into out_dir; a file that two builds both have must be the same in each"""
    for dist in dists:
        for source in sorted(p for p in dist.rglob("*") if p.is_file()):
            dest = out_dir / source.relative_to(dist)
            if dest.exists():
                if not filecmp.cmp(source, dest, shallow=False):
                    raise SystemExit(f"ERROR: {source.relative_to(dist)} differs between the builds")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)


def is_ucrt(name):
    name = name.lower()
    return name == "ucrtbase.dll" or name.startswith("api-ms-win-")


# Licenses: ours next to the exes; in licenses\\, Python's (which also covers what it includes: OpenSSL,
# bzip2, libffi, the Microsoft runtime), Nuitka's (its runtime library is in each exe), those of each package
# with modules in the build, from its .dist-info folder, and the window's font's.
LICENSE_FILE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
# PySide6's and shiboken6's .dist-info hold only Qt's commercial license text: their LGPL texts, where their
# source code is, and the notices of the third-party code in Qt are in assets/licenses/PySide6/ instead (written by
# dev-tools/qt-licenses.py, its README by hand)
QT_DISTRIBUTIONS = {"PySide6_Essentials", "shiboken6"}
QT_LICENSES = ROOT / "assets" / "licenses" / "PySide6"
FONT_LICENSE = ROOT / "smoothui" / "merger" / "fonts" / "OFL.txt"


def is_lgpl(dist):
    meta = dist.metadata
    text = " ".join([meta.get("License-Expression") or "", meta.get("License") or "", *(meta.get_all("Classifier") or [])])
    return "LGPL" in text or "Lesser General Public" in text


def bundled_distributions(dists):
    """The installed distributions (packages) that have modules in the builds, from Nuitka's reports on them"""
    names = {module.get("distribution") for dist in dists
             for module in ElementTree.parse(dist.parent / "report.xml").getroot().iter("module")}
    names.discard(None)
    return sorted((importlib.metadata.distribution(name) for name in names), key=lambda d: d.metadata["Name"].lower())


def license_files(dist):
    """The license files in a distribution's .dist-info folder (or its licenses\\ subfolder)"""
    return [f for f in dist.files or []
            if f.parts[0].endswith(".dist-info") and f.name.upper().startswith(LICENSE_FILE_PREFIXES)]


def write_licenses(out_dir, dists):
    licenses_dir = out_dir / "licenses"
    python_version = platform.python_version()
    (licenses_dir / "Python").mkdir(parents=True)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses_dir / "Python" / "LICENSE.txt")
    readme = [
        f"Third-party software in {PRODUCT}'s tools; each one's license is in the folder of the same name.",
        "",
        f"Python {python_version}, with the libraries it includes (OpenSSL, bzip2, libffi): https://www.python.org/",
    ]
    for dist in [importlib.metadata.distribution("nuitka"), *bundled_distributions(dists)]:
        name, version = dist.metadata["Name"], dist.version
        if name in QT_DISTRIBUTIONS:
            (licenses_dir / "PySide6").mkdir(exist_ok=True)
            for f in sorted(QT_LICENSES.iterdir()):
                shutil.copyfile(f, licenses_dir / "PySide6" / f.name)
            readme.append(f"{name} {version} (LGPL; see PySide6\\README.txt): https://pypi.org/project/{name}/{version}/")
            continue
        # The LGPL lets users replace the library with a modified version, which Nuitka compiles into the exe
        # (unlike Qt's, PySide6's and shiboken6's DLLs and modules, see their README)
        if is_lgpl(dist):
            raise SystemExit(f"ERROR: {name} {version} (LGPL) is compiled into the build, so it can't be replaced")
        files = license_files(dist)
        if not files:
            raise SystemExit(f"ERROR: {name} {version} is in the build but has no license file in its .dist-info")
        (licenses_dir / name).mkdir()
        for f in files:
            shutil.copyfile(dist.locate_file(f), licenses_dir / name / f.name)
        if name.lower() == "nuitka":
            readme.append(f"{name} {version} (its runtime library, in each exe; AGPLv3 with the Nuitka Runtime Library "
                          f"Exception, LICENSE-RUNTIME.txt): https://pypi.org/project/{name}/{version}/")
        else:
            readme.append(f"{name} {version}: https://pypi.org/project/{name}/{version}/")
    (licenses_dir / "Roboto").mkdir()
    shutil.copyfile(FONT_LICENSE, licenses_dir / "Roboto" / FONT_LICENSE.name)
    readme.append("Roboto 3.016 (the merger window's font): https://github.com/googlefonts/roboto-3-classic")
    (licenses_dir / "README.txt").write_text("\n".join(readme) + "\n", encoding="utf-8", newline="\r\n")


def write_zip(built, zip_path):
    """Zip the build as a release ships it, in the folder ZIP_FOLDER"""
    files = sorted(p for p in built.rglob("*") if p.is_file())
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files:
            z.write(path, f"{ZIP_FOLDER}/{path.relative_to(built).as_posix()}")
    print(f"Wrote {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.1f} MiB, {len(files)} files)")


def mirror(built, target):
    """Make target the same as built, except for its data\\ (robocopy: copies only what changed)"""
    # /XD with a full path: only that folder. Exit codes below 8 mean success. Its output (which lists each file
    # it deletes) only on failure.
    command = ["robocopy", str(built), str(target), "/MIR", "/XD", str(target / "data"), "/R:2", "/W:1",
               "/NJH", "/NJS", "/NDL", "/NFL", "/NP"]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    code = result.returncode
    if code >= 8:
        print(result.stdout, result.stderr, sep="", end="")
        raise SystemExit(f"ERROR: robocopy failed (exit code {code}): is SmoothUILauncher.exe or SmoothUIMerger.exe running?")
    print(f"Mirrored into: {target}")


def main():
    args = parse_args()
    if in_wsl():
        run_on_windows(args)
    check_python()
    out_dir = (args.out or BUILD_DIR).resolve()
    if not out_dir.is_relative_to(ROOT / "build"):
        raise SystemExit(f"ERROR: --out must be inside {ROOT / 'build'} (it is deleted first), not {out_dir}")
    version = read_tools_version(args.version)
    print(f"Tools version {version}")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    version_file = WORK_DIR / "tools_version.txt"
    version_file.write_text(version + "\n", encoding="utf-8")

    launcher = nuitka("launcher.py", "SmoothUILauncher.exe", "Smooth UI Remastered launcher", "Launcher.ico", version, [
        f"--nofollow-import-to={','.join(LAUNCHER_EXCLUDES)}",
        f"--include-data-files={version_file}={VERSION_DEST}",
    ])
    check_launcher(launcher)
    merger = nuitka("merger-gui.py", "SmoothUIMerger.exe", "Smooth UI Remastered merger", "Merger.ico", version, [
        "--enable-plugin=pyside6", "--include-qt-plugins=platforms,styles,imageformats", "--noinclude-qt-translations",
        f"--nofollow-import-to={','.join(MERGER_EXCLUDES)}", *qt_dropped(), *merger_data_files(version_file),
    ])

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    combine([launcher, merger], out_dir)
    if not (out_dir / QT_PLUGIN_DEST / "platforms" / "qwindows.dll").is_file():
        raise SystemExit("ERROR: the build has no Qt Windows platform plugin (qwindows.dll)")

    # The Universal CRT is part of Windows 10 and later, so it's never shipped. (A build may collect any copy
    # it finds in PATH, e.g. a JDK's.) VCRUNTIME140.dll is not part of Windows, and stays.
    for p in [p for p in out_dir.rglob("*.dll") if is_ucrt(p.name)]:
        p.unlink()

    # Our own files go next to the exes. copyfile, not copy2: no metadata, which fails on some drives.
    manifests = sorted(
        p for p in (ROOT / "manifests").glob("*.json")
        if (args.dev_manifests or not fnmatch(p.name, "*dev*")) and p.name != "v0.2.json"
    )
    if not manifests:
        raise SystemExit("ERROR: no release manifests found in manifests/")
    dev_manifests = [p.name for p in manifests if fnmatch(p.name, "*dev*")]
    if dev_manifests:
        print(f"Dev manifests included (a test build, not for a release): {', '.join(dev_manifests)}")
    (out_dir / "manifests").mkdir()
    for p in manifests:
        shutil.copyfile(p, out_dir / "manifests" / p.name)
    # The reviews of mods' code (see smoothui/mod_reviews.py)
    shutil.copyfile(ROOT / "manifests" / "reviewed_mod_warnings.txt", out_dir / "manifests" / "reviewed_mod_warnings.txt")
    shutil.copyfile(ROOT / "LICENSE.txt", out_dir / "LICENSE.txt")
    shutil.copyfile(ROOT / "README_tools.md", out_dir / "README_tools.md")
    write_licenses(out_dir, [launcher, merger])
    (out_dir / SHIPPED_DEST).write_text("".join(f"{p.name}\n" for p in sorted(out_dir.iterdir())), encoding="utf-8")
    print(f"Built: {out_dir}")

    if args.zip is not None:
        write_zip(out_dir, Path(args.zip) if args.zip else ROOT / "dist" / f"SmoothUITools-{version}-test.zip")
    elif args.out is None:
        mirror(out_dir, INSTALL_DIR)


if __name__ == "__main__":
    main()
