"""Scan the installed mods: for each file the installed Smooth UI build patches, which mod's copy the game
loads (see load_order.py), and what keeps a mod or file from being patched.

Read-only, and reads only bundles' tables of contents (except to compare two copies of a file within one
mod). It must work through MO2's virtual file system, where exists() and is_dir() often say no for files
that are there: folders are listed with os.scandir, and files are opened rather than checked.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..common import IN_WSL, SmoothUIError, fail, profile_key, to_native, to_windows
from ..patcher.patcher_core import SourceFile
from ..witcher3.bundle import read_toc
from ..witcher3.mod_bundles import read_bundle_tocs
from ..witcher3.mods_settings import read_mods_settings
from .installed import Installed, installed_build, is_smoothui_folder
from .load_order import Mod, load_order, make_mods, undetermined_for

log = logging.getLogger(__name__)

# The merger's own package (the name isn't final)
PACKAGE_FOLDER = "mod0000_SmoothUI_MergedPatches"
# Compat patches built by build-compat.py (see compat.py's mod_folder_name)
COMPAT_PATCH = re.compile(r"mod000_SmoothUIRemastered_.+_Compat", re.IGNORECASE | re.DOTALL)
# The longest path of a mod's file (its metadata.store or a bundle) known to load properly. Seen in game: a
# mod whose metadata.store path was 259 characters loaded; one whose bundle path was 260 had its screen
# show blank, and a 255-character folder name made the game ignore the mod.
MAX_PATH_LENGTH = 259
# The game's mod.io mods: <%PUBLIC%>\mod.io\8254\mods\ (8254 is the game's id on mod.io)
MODIO_MODS = Path("mod.io", "8254", "mods")

class ProblemKind(Enum):
    UNREADABLE = "unreadable"           # a mod whose bundles can't be read: left out
    LONG_PATH = "long path"             # a mod whose files' paths are too long for the game: left out
    NOT_LOADED = "not loaded"           # bundles without metadata.store: the game ignores them
    LOOSE_FILES = "loose files"         # loose .redswf files in content\: the game ignores them
    UNDETERMINED = "undetermined"       # the load order doesn't decide a file's winner
    COMPAT_INSTALLED = "compat patch"   # an installed compat patch: not needed with the package
    ORPHANED_COMPAT = "orphaned compat patch"   # the game loads a compat patch's copy that no other mod ships
    MODIO = "mod.io"                    # a mod.io mod ships a file we patch, or mod.io couldn't be checked


@dataclass(frozen=True, slots=True)
class Problem:
    kind: ProblemKind
    message: str
    mod: str | None = None      # the mod's folder in Mods\
    file: str | None = None     # the file (profile key)


@dataclass(slots=True)
class FileResult:
    """One file the installed Smooth UI build patches"""
    path: str                                   # profile key
    shippers: list[Mod] = field(default_factory=list)   # the enabled mods shipping it, ours included, in load
                                                # order: the first one's copy is the one the game loads now
    winner: SourceFile | None = None            # the copy to patch: that of the first shipper that isn't ours
                                                # (see is_own_folder; a compat patch is already patched). None
                                                # if no other mod ships it (Smooth UI's own copy covers it), or
                                                # if undetermined
    undetermined: list[Mod] = field(default_factory=list)   # see load_order.undetermined_for

    @property
    def loaded(self) -> Mod | None:
        """The mod whose copy the game loads now"""
        return self.shippers[0] if self.shippers else None

    @property
    def loaded_without_package(self) -> Mod | None:
        """The mod whose copy the game loads where the package doesn't hold the file"""
        return next((mod for mod in self.shippers if not is_package_folder(mod.folder)), None)


@dataclass(slots=True)
class Scan:
    mods_dir: Path | None                       # the game's Mods\ folder, None if it has none
    installed: Installed
    order: list[Mod]                            # the enabled mods, in load order, ours included
    files: dict[str, FileResult]                # by profile key; empty unless installed is usable
    problems: list[Problem]


def is_own_folder(name: str) -> bool:
    """Whether a mod folder is ours: Smooth UI itself, the package, or a compat patch"""
    return is_smoothui_folder(name) or is_package_folder(name) or COMPAT_PATCH.fullmatch(name) is not None


def is_package_folder(name: str) -> bool:
    return name.lower() == PACKAGE_FOLDER.lower()


def scan(game_path: Path, mods_settings: Path, *, modio_root: Path | None = None, manifests=None) -> Scan:
    """Scan the game's Mods\\ folder, with the load order from mods_settings (the path of mods.settings).

    modio_root is the folder holding mod.io's mods (default: found from %PUBLIC%); manifests ({version:
    manifest}) replace the release manifests the merger has, for tests. Fails (SmoothUIError) only if
    mods.settings can't be read; a mod that can't be read is a problem, and is left out."""
    mods_dir = _mods_dir(game_path)
    folders = _list_names(mods_dir) if mods_dir else []
    mods = make_mods(folders, read_mods_settings(mods_settings))
    order = load_order(mods)
    installed = installed_build(mods_dir, folders, mods, manifests)
    if not installed.usable:
        return Scan(mods_dir, installed, order, {}, [])

    keys = {profile_key(key) for key in installed.manifest["files"]}
    problems = []
    shipped = {}    # Mod: {profile key: (bundle, TocEntry)}
    mods_dir_length = windows_path_length(mods_dir)
    for mod in order:
        if COMPAT_PATCH.fullmatch(mod.folder):
            # Whether it can be removed depends on what the package holds: see builder.compat_notes
            problems.append(Problem(ProblemKind.COMPAT_INSTALLED, f"{mod.folder} is a Smooth UI compat patch: the package "
                                    "replaces it for every file the package holds", mod=mod.folder))
        shipped[mod] = _mod_files(mods_dir / mod.folder, mod, keys, problems, mods_dir_length)

    files = {}
    for key in sorted(keys):
        result = files[key] = FileResult(key, [mod for mod in order if key in shipped[mod]])
        others = [mod for mod in result.shippers if not is_own_folder(mod.folder)]
        result.undetermined = undetermined_for(others)
        if COMPAT_PATCH.fullmatch(result.loaded.folder if result.loaded else "") and not others:
            # Not necessarily a mistake: a mod that only ships .redswf files works through its compat patch alone
            problems.append(Problem(ProblemKind.ORPHANED_COMPAT, f"{result.loaded.folder} ships {key}, which no other "
                                    "enabled mod ships, and the game loads its copy. That is fine if you use the "
                                    "compat patch in place of the mod it was made for. If that mod was disabled or "
                                    "removed by mistake, enable it again, or remove the compat patch",
                                    result.loaded.folder, key))
        if result.undetermined:
            names = ", ".join(mod.folder for mod in result.undetermined)
            problems.append(Problem(ProblemKind.UNDETERMINED, f"the load order can't be determined for {key}: it is shipped by "
                                    f"{names}, and at least one of them has a mods.settings entry without a "
                                    "Priority. Give it a Priority", file=key))
        elif others:
            winner = others[0]
            result.winner = SourceFile.in_bundle(key, winner.folder, *shipped[winner][key])
    problems += modio_problems(keys, modio_root)
    return Scan(mods_dir, installed, order, files, problems)


def _mods_dir(game_path):
    """The game's Mods\\ folder (spelled as on disk), or None if there is none"""
    try:
        return next((Path(game_path) / name for name in _list_names(game_path) if name.lower() == "mods"), None)
    except OSError as e:
        fail(f"can't read the game folder {game_path}: {e}")


def windows_path_length(path) -> int:
    """The length of path as Windows spells it, which the game's limits apply to"""
    return len(to_windows(path)) if IN_WSL else len(str(path))


def _list_names(folder):
    with os.scandir(folder) as entries:
        return [entry.name for entry in entries]


def _mod_files(mod_dir, mod, keys, problems, mods_dir_length):
    """{profile key: (bundle, TocEntry)} for the files in keys that a mod ships in bundles the game loads;
    what keeps others from counting goes into problems. mods_dir_length: see windows_path_length."""
    content = mod_dir / "content"
    content_length = mods_dir_length + len(f"\\{mod.folder}\\content\\")
    try:
        listing = _list_names(content)
    except (FileNotFoundError, NotADirectoryError):
        return {}   # not a mod folder, or one without files the game loads
    except OSError as e:
        if content_length + len("metadata.store") > MAX_PATH_LENGTH:
            problems.append(_long_path(mod, "metadata.store", content_length + len("metadata.store")))
        else:
            problems.append(Problem(ProblemKind.UNREADABLE, f"{mod.folder} can't be read, and is left out: {e}", mod=mod.folder))
        return {}

    names = {name.lower() for name in listing}
    longest = max((name for name in names if name.endswith(".bundle") or name == "metadata.store"), key=len, default="")
    if longest and content_length + len(longest) > MAX_PATH_LENGTH:
        problems.append(_long_path(mod, longest, content_length + len(longest)))
        return {}

    for loose in _loose_files(content, listing, keys):
        problems.append(Problem(ProblemKind.LOOSE_FILES, f"{mod.folder} ships {loose} as a loose file, which the game ignores "
                                "(only files in bundles are loaded), so it isn't patched", mod.folder, loose))
    if not any(name.endswith(".bundle") for name in names):
        return {}
    if "metadata.store" not in names:
        problems.append(Problem(ProblemKind.NOT_LOADED, f"{mod.folder} has bundles but no metadata.store, so the game doesn't "
                                "load them; left out", mod=mod.folder))
        return {}
    try:
        return read_bundle_tocs(content, keys)
    except SmoothUIError as e:
        problems.append(Problem(ProblemKind.UNREADABLE, f"{mod.folder} is left out: {e}", mod=mod.folder))
        return {}


def _long_path(mod, file, length):
    return Problem(ProblemKind.LONG_PATH, f"{mod.folder} is left out: its folder name is too long. The path of its {file} is "
                   f"{length} characters, and with paths over {MAX_PATH_LENGTH} the game ignores a mod or shows "
                   "its screens blank. Give the folder a shorter name", mod=mod.folder)


def _loose_files(content, listing, keys):
    """The files in keys that a content folder (whose names are listing) holds as loose files. Only the
    folders on the way to a key are listed: on a hard disk, each folder listed costs a seek, and script
    mods have many."""
    on_the_way = {}     # a folder (relative to content, lowercased, "" for content): the names in it that lead to keys
    for key in keys:
        parts = key.split("/")
        for depth in range(len(parts)):
            on_the_way.setdefault("/".join(parts[:depth]), set()).add(parts[depth])
    found = []
    pending = [(Path(content), "", listing)]
    while pending:
        folder, relative, names = pending.pop()
        for name in names:
            if name.lower() not in on_the_way.get(relative, ()):
                continue
            path = f"{relative}/{name.lower()}" if relative else name.lower()
            if path in keys:
                found.append(path)
                continue
            try:
                pending.append((folder / name, path, _list_names(folder / name)))
            except OSError:
                pass    # not a folder, or one that can't be read (as os.walk would skip it)
    return sorted(found)


def modio_problems(keys, modio_root=None) -> list[Problem]:
    """A warning for each file in keys that a mod.io mod ships. mod.io mods always load before anything in
    Mods\\, so no package can patch them. Their files aren't parsed (they can't be tested here): every
    .bundle under modio_root (default: the game's mod.io mods folder) counts. Never fails."""
    try:
        root = modio_root if modio_root is not None else _modio_root()
        if root is None:
            return []
        problems = []
        for bundle in _modio_bundles(root):
            for entry in read_toc(bundle, ".redswf"):
                key = profile_key(entry.path)
                if key in keys:
                    problems.append(Problem(ProblemKind.MODIO, f"a mod.io mod ships {key} ({bundle}); mod.io mods always load "
                                            "before anything in Mods\\, so it can't be patched. Install it from "
                                            "Nexus Mods instead (it may be disabled in game)", file=key))
        return problems
    except Exception as e:  # noqa: BLE001 (a check we can't test against real mod.io files: never fail the scan)
        return [Problem(ProblemKind.MODIO, f"couldn't check mod.io mods: {e}")]


def _modio_root():
    """<%PUBLIC%>\\mod.io\\8254\\mods; None where there is no Windows to ask"""
    if sys.platform == "win32":
        return Path(os.environ["PUBLIC"]) / MODIO_MODS
    if IN_WSL:
        public = subprocess.run(["cmd.exe", "/d", "/c", "echo %PUBLIC%"], capture_output=True, text=True,
                                check=True, cwd="/mnt/c").stdout.strip()
        return to_native(public) / MODIO_MODS
    return None


def _modio_bundles(root):
    """Every .bundle under root (none if root doesn't exist)"""
    def raise_error(e):
        if not (isinstance(e, FileNotFoundError) and Path(e.filename) == Path(root)):
            raise e
    return sorted(Path(folder, name) for folder, _, names in os.walk(root, onerror=raise_error)
                  for name in names if name.lower().endswith(".bundle"))
