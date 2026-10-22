"""Where to read one file to patch: a loose file, or an entry in a bundle (see SourceFile)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from .bundle import TocEntry, read_entry

# The origin of files from the game itself (see SourceFile)
GAME_FILES = "game files"


class SourceFile(NamedTuple):
    """One file to patch: where to read it. Whoever patches it (a worker, see patcher_core.patch_files)
    reads it, so the main process never holds the files' data. Make one with loose() or in_bundle()."""
    path: str       # its depot path as profiles spell it: lowercase, / separated (see profile_key)
    origin: str     # where it came from: GAME_FILES, or the mod's folder
    file: Path      # the loose file, or the bundle holding it
    size: int       # of its content
    toc_entry: TocEntry | None = None   # its entry in that bundle (see bundle.read_toc); None for a loose file

    @classmethod
    def loose(cls, path, origin, file):
        """A loose file (raises OSError if it can't be read)"""
        return cls(path, origin, Path(file), os.path.getsize(file))

    @classmethod
    def in_bundle(cls, path, origin, bundle, toc_entry):
        """A file in a bundle, by its TOC entry"""
        return cls(path, origin, Path(bundle), toc_entry.size, toc_entry)

    def read(self) -> bytes:
        if self.toc_entry is None:
            return self.file.read_bytes()
        with open(self.file, "rb") as f:
            return read_entry(f, self.toc_entry).content()
