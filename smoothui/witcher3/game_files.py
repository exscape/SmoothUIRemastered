"""The game's own copies of files, read from its content bundles in place (only the tables of contents, until a
file is patched): what the merger patches where no mod changes a file, for the files a release doesn't ship.

Light: the merger's launch check may use it (no patcher imports).

The game's bundles are in content\\content*\\bundles\\ (Remastered keeps everything in content0) and
dlc\\*\\content\\. Where two of them hold different copies of a file, which one the game uses isn't known
here, so that fails. Folders are listed with os.scandir, which works through MO2's virtual file system.
"""

from __future__ import annotations

import os
from pathlib import Path

from .mod_bundles import read_bundle_tocs
from .source_file import GAME_FILES, SourceFile


def content_dirs(game_path) -> list[Path]:
    """The folders holding the game's own bundles, in a stable order"""
    game_path = Path(game_path)
    found = [game_path / "content" / name / "bundles" for name in _folders(game_path / "content")
             if name.lower().startswith("content")]
    found += [game_path / "dlc" / name / "content" for name in _folders(game_path / "dlc")]
    return [folder for folder in found if folder.name in _folders(folder.parent)]


def game_files(game_path, keys) -> dict[str, SourceFile]:
    """{profile key: SourceFile} for those of keys (profile keys) that the game's bundles hold. Fails
    (SmoothUIError) if a bundle can't be read, or if two hold different copies of a file."""
    return {key: SourceFile.in_bundle(key, GAME_FILES, bundle, entry)
            for key, (bundle, entry) in read_bundle_tocs(content_dirs(game_path), set(keys)).items()}


def _folders(path: Path) -> list[str]:
    """The names of the folders in path, sorted; none if it doesn't exist"""
    try:
        with os.scandir(path) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir())
    except (FileNotFoundError, NotADirectoryError):
        return []
