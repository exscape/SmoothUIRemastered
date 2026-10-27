"""Scan the installed mods: for each file the installed Smooth UI build patches, which mod's copy the game
loads (see load_order.py), and what keeps a mod or file from being patched. For the files the build doesn't
ship (a group only the merger builds, see build/profile.py's Group), the game's own copy is the one to patch
where no mod changes the file; a file a mod adds to a group's folder is the group's too (see Scan.group_of).

Read-only, and reads only bundles' tables of contents (except to compare two copies of a file within one
mod). It must work through MO2's virtual file system, where exists() and is_dir() often say no for files
that are there: folders are listed with os.scandir, and files are opened rather than checked.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..common import IN_WSL, SmoothUIError, fail, profile_key, to_native, to_windows
from ..witcher3.bundle import read_toc
from ..witcher3.game_files import game_files
from ..witcher3.mod_bundles import read_bundle_tocs
from ..witcher3.mods_settings import ModSettings, read_mods_settings
from ..witcher3.source_file import SourceFile
from .installed import Installed, installed_build, is_smoothui_folder
from .load_order import Mod, compare, load_order, make_mods, undetermined_for
from .screens import screen_name

log = logging.getLogger(__name__)

# The merger's own package (the name isn't final)
PACKAGE_FOLDER = "mod0000_SmoothUI_MergedPatches"
# Script Merger's mod: scripts and text files only, so it may load before the package
SCRIPT_MERGER_FOLDER = "mod0000_MergedFiles"
# Compat patches built by build-compat.py (see compat.py's mod_folder_name); the short name is accepted too
COMPAT_PATCH = re.compile(r"mod000_SmoothUI(Remastered)?_.+_Compat", re.IGNORECASE | re.DOTALL)
# The longest path of a mod's file (its metadata.store or a bundle) known to load properly. Seen in game: a
# mod whose metadata.store path was 259 characters loaded; one whose bundle path was 260 had its screen
# show blank, and a 255-character folder name made the game ignore the mod.
MAX_PATH_LENGTH = 259
# The game's mod.io mods: <%PUBLIC%>\mod.io\8254\mods\ (8254 is the game's id on mod.io)
MODIO_MODS = Path("mod.io", "8254", "mods")

class ProblemKind(Enum):
    MOD_ISSUE = "mod issue"             # a mod that can't be used, and is left out (the message says why: its
                                        # bundles can't be read, its paths are too long for the game, or it
                                        # has bundles but no metadata.store)
    UNDETERMINED = "undetermined"       # the load order doesn't decide a file's winner
    ORPHANED_COMPAT = "orphaned compat patch"   # the game loads a compat patch's copy that no other mod ships
    MODIO = "mod.io"                    # a mod.io mod ships a file we patch, or mod.io couldn't be checked
    GAME_FILES = "game files"           # the game's own copy of a file only the merger builds can't be read


@dataclass(frozen=True, slots=True)
class Problem:
    kind: ProblemKind
    message: str
    mod: str | None = None      # the mod's folder in Mods\
    file: str | None = None     # the file (profile key)


@dataclass(slots=True)
class FileResult:
    """One file the installed Smooth UI build patches (or a file a mod adds to one of its groups)"""
    path: str                                   # profile key
    shippers: list[Mod] = field(default_factory=list)   # the enabled mods shipping it, ours included, in load
                                                # order: the first one's copy is the one the game loads now
    winner: SourceFile | None = None            # the copy to patch: that of the first shipper that isn't ours
                                                # (see is_own_folder; a compat patch is already patched), or for a
                                                # file the build doesn't ship, the game's own copy if no other mod
                                                # ships it. None if no other mod ships a shipped file (Smooth UI's
                                                # own copy covers it), if undetermined, or if the game's copy
                                                # can't be read
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
    # For an enabled Smooth UI the merger can't use: the other enabled mods that ship a .redswf file in its
    # bundles (their screens may be wrong until the merger can patch for it)
    conflicts: list[str] = field(default_factory=list)
    # {profile key: group} for the files of the build's groups (see build/profile.py's Group), and the .redswf
    # files that mods add to a group's folder (e.g. a mod's own HUD module): those play at the group's frame
    # rate too, so they are the group's files, patched with its settings (see release.with_unlisted_files)
    group_of: dict[str, str] = field(default_factory=dict)
    # The groups the user turned off (see game.groups_off): their files have no winner, so a build leaves
    # them out, and the game shows its own copies (or the mods')
    groups_off: frozenset[str] = frozenset()

    @property
    def compat_patches(self) -> list[str]:
        """The installed compat patches (enabled ones, in load order): not needed for the files the package
        holds (see builder.compat_notes)"""
        return [mod.folder for mod in self.order if COMPAT_PATCH.fullmatch(mod.folder)]

    def removable_compat(self, held: set[str]) -> list[str]:
        """The installed compat patches whose every file the merged patches hold (profile keys): they can be
        removed. One the game still uses for a file they don't hold (a screen the merger left out) isn't."""
        return [folder for folder in self.compat_patches
                if all(key in held for key, file in self.files.items()
                       if any(mod.folder == folder for mod in file.shippers))]


def is_own_folder(name: str) -> bool:
    """Whether a mod folder is ours: Smooth UI itself, the package, or a compat patch"""
    return is_smoothui_folder(name) or is_package_folder(name) or COMPAT_PATCH.fullmatch(name) is not None


def is_package_folder(name: str) -> bool:
    return name.lower() == PACKAGE_FOLDER.lower()


def entry_needed(order: list[Mod], ours: ModSettings | None) -> bool:
    """Whether the package, with its current mods.settings section (ours; None if it has none), needs the
    entry a build writes: some enabled mod in order loads before it. Script Merger's mod may (it holds no
    .redswf files, so it never conflicts), and the package may have any valid priority, as long as it loads
    first."""
    if ours is not None and (not ours.enabled or ours.priority is None):
        return True
    package = Mod(PACKAGE_FOLDER, ours)
    return any(compare(mod, package) < 0 for mod in order
               if not is_package_folder(mod.folder) and mod.folder.lower() != SCRIPT_MERGER_FOLDER.lower())


def scan(game_path: Path, mods_settings: Path, *, groups_off=frozenset(), modio_root: Path | None = None,
         manifests=None) -> Scan:
    """Scan the game's Mods\\ folder, with the load order from mods_settings (the path of mods.settings).
    groups_off: see Scan.groups_off.

    modio_root is the folder holding mod.io's mods (default: found from %PUBLIC%); manifests ({version:
    manifest}) replace the release manifests the merger has, for tests. Fails (SmoothUIError) only if
    mods.settings can't be read; a mod that can't be read is a problem, and is left out."""
    mods_dir = _mods_dir(game_path)
    folders = _list_names(mods_dir) if mods_dir else []
    mods = make_mods(folders, read_mods_settings(mods_settings))
    order = load_order(mods)
    installed = installed_build(mods_dir, folders, mods, manifests)
    if not installed.usable:
        return Scan(mods_dir, installed, order, {}, [], _conflicts(mods_dir, order), groups_off=frozenset(groups_off))

    records = {profile_key(key): record for key, record in installed.manifest["files"].items()}
    keys = set(records)
    unshipped = {key for key, record in records.items() if not record.get("shipped", True)}
    off = {key for key, record in records.items() if record.get("group") in groups_off}
    # The folders of the groups' files, but those turned off: {folder: group}
    group_dirs = {posixpath.dirname(key): record["group"] for key, record in records.items()
                  if "group" in record and key not in off}
    # The build's files, and any others in its groups' folders (files that mods add to a group)
    wanted = lambda key: key in keys or posixpath.dirname(key) in group_dirs
    problems = []
    shipped = {}    # Mod: {profile key: (bundle, TocEntry)}
    mods_dir_length = windows_path_length(mods_dir)
    for mod in order:
        # The package's own problems aren't the scan's: package_state tells them, and a build replaces it
        shipped[mod] = _mod_files(mods_dir / mod.folder, mod, wanted, [] if is_package_folder(mod.folder) else problems,
                                  mods_dir_length)
    group_of = {key: record["group"] for key, record in records.items() if "group" in record}
    group_of |= {key: group_dirs[posixpath.dirname(key)] for mod, found in shipped.items() if not is_own_folder(mod.folder)
                 for key in found if key not in keys}
    game = _game_copies(game_path, unshipped - off, problems)

    files = {}
    for key in sorted(group_of.keys() | keys):
        result = files[key] = FileResult(key, [mod for mod in order if key in shipped[mod]])
        others = [mod for mod in result.shippers if not is_own_folder(mod.folder)]
        result.undetermined = undetermined_for(others) if key not in off else []
        if COMPAT_PATCH.fullmatch(result.loaded.folder if result.loaded else "") and not others:
            # The merger replaces these compat patches, so one that the game still uses should go
            problems.append(Problem(ProblemKind.ORPHANED_COMPAT,
                                    f"{result.loaded.folder} is an old Smooth UI compat patch, which the merger "
                                    "replaces: remove it", result.loaded.folder, key))
        if result.undetermined:
            names = ", ".join(mod.folder for mod in result.undetermined)
            problems.append(Problem(ProblemKind.UNDETERMINED, f"the load order doesn't decide whose {screen_name(key)} the "
                                    f"game uses: {names} change it, and at least one of them has a mods.settings entry "
                                    "without a Priority. Give it one", file=key))
        elif key in off:
            pass
        elif others:
            winner = others[0]
            result.winner = SourceFile.in_bundle(key, winner.folder, *shipped[winner][key])
        elif key in unshipped:
            result.winner = game.get(key)
    problems += modio_problems(keys, modio_root)
    return Scan(mods_dir, installed, order, files, problems, group_of=group_of, groups_off=frozenset(groups_off))


def _game_copies(game_path, keys, problems) -> dict[str, SourceFile]:
    """{profile key: SourceFile} for the game's own copies of keys; what's missing goes into problems"""
    if not keys:
        return {}
    try:
        found = game_files(game_path, keys)
    except SmoothUIError as e:
        problems.append(Problem(ProblemKind.GAME_FILES, f"the game's own files can't be read: {e}"))
        return {}
    for key in sorted(keys - found.keys()):
        problems.append(Problem(ProblemKind.GAME_FILES, f"the game has no {screen_name(key)} ({key}): is the game "
                                "installed fully, and of the version this Smooth UI is for?", file=key))
    return found


def _conflicts(mods_dir, order) -> list[str]:
    """Scan.conflicts (Smooth UI's files are those in its own bundles: the merger has no manifest for it)"""
    keys = set()
    for mod in order:
        if is_smoothui_folder(mod.folder):
            try:
                keys |= {key for key in read_bundle_tocs(mods_dir / mod.folder / "content") if key.endswith(".redswf")}
            except SmoothUIError:
                pass    # installed says why it can't be used
    return [mod.folder for mod in order if keys and not is_own_folder(mod.folder)
            and _mod_files(mods_dir / mod.folder, mod, keys, [], windows_path_length(mods_dir))]


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
        names = {name.lower() for name in _list_names(content)}
    except (FileNotFoundError, NotADirectoryError):
        return {}   # not a mod folder, or one without files the game loads
    except OSError as e:
        if content_length + len("metadata.store") > MAX_PATH_LENGTH:
            problems.append(_long_path(mod, "metadata.store", content_length + len("metadata.store")))
        else:
            problems.append(Problem(ProblemKind.MOD_ISSUE, f"{mod.folder} can't be read, and is left out: {e}", mod=mod.folder))
        return {}

    longest = max((name for name in names if name.endswith(".bundle") or name == "metadata.store"), key=len, default="")
    if longest and content_length + len(longest) > MAX_PATH_LENGTH:
        problems.append(_long_path(mod, longest, content_length + len(longest)))
        return {}
    if not any(name.endswith(".bundle") for name in names):
        return {}
    if "metadata.store" not in names:
        problems.append(Problem(ProblemKind.MOD_ISSUE, f"{mod.folder} has bundles but no metadata.store, so the game doesn't "
                                "load them; left out", mod=mod.folder))
        return {}
    try:
        return read_bundle_tocs(content, keys, ".redswf")
    except SmoothUIError as e:
        log.warning(f"{mod.folder} is left out: {e}")     # (the full text, with the path, is for the log)
        reason = (f"its bundle files can't be read{' (not a Witcher 3 bundle)' if 'POTATO70' in str(e) else ''}"
                  if str(e).startswith("can't read the bundles") else e)
        problems.append(Problem(ProblemKind.MOD_ISSUE, f"{mod.folder} is left out: {reason}", mod=mod.folder))
        return {}


def _long_path(mod, file, length):
    return Problem(ProblemKind.MOD_ISSUE, f"{mod.folder} is left out: its folder name is too long. The path of its {file} is "
                   f"{length} characters, and with paths over {MAX_PATH_LENGTH} the game ignores a mod or shows "
                   "its screens blank. Give the folder a shorter name", mod=mod.folder)


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
                    problems.append(Problem(ProblemKind.MODIO, f"a mod.io mod changes {screen_name(key)} ({bundle}): mod.io "
                                            "mods always load first, so it can't be patched. Install it from Nexus Mods "
                                            "instead (it may be disabled in game)", file=key))
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
