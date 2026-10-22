"""Read the files a mod ships in its content folder's bundles, without unpacking them, and pack files
into a mod's bundle and metadata.store.

Reading is used by the compat build (one mod, unpacked from its archive) and the merger's scan (each
installed mod); packing by the builds and the merger's package.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..common import fail, profile_key
from .bundle import BundleError, TocEntry, read_entry, read_toc, write_bundle
from .metadata_store import MetadataStoreError, make_metadata_store


def list_bundles(content_dir: Path) -> list[Path]:
    """The .bundle files in content_dir, sorted. Listed with os.scandir, which works through MO2's virtual
    file system."""
    return sorted(Path(content_dir) / entry.name for entry in os.scandir(content_dir)
                  if entry.name.lower().endswith(".bundle"))


def read_bundle_tocs(content_dir: Path, keys=None) -> dict[str, tuple[Path, TocEntry]]:
    """{profile key: (bundle file, TocEntry)} for the files in the bundle(s) in content_dir, reading only
    their tables of contents (see read_entry_content for a file's content). With keys (profile keys), only
    those files; if they all have one extension, the other files' entries aren't read (see
    bundle.read_toc). Fails (SmoothUIError) if a bundle can't be read, or if two bundles hold different
    copies of one file: which one the game would use is unknown."""
    extensions = {os.path.splitext(key)[1] for key in keys} if keys is not None else set()
    extension = (extensions.pop() or None) if len(extensions) == 1 else None
    try:
        files = {}
        for bundle in list_bundles(content_dir):
            for entry in read_toc(bundle, extension):
                key = profile_key(entry.path)
                if keys is not None and key not in keys:
                    continue
                if key in files and read_entry_content(*files[key]) != read_entry_content(bundle, entry):
                    fail(f"{entry.path} is in both {files[key][0].name} and {bundle.name}, with different contents")
                files[key] = (bundle, entry)
    except (BundleError, OSError) as e:
        fail(f"can't read the bundles in {content_dir}: {e}")
    return files


def read_entry_content(bundle: Path, toc_entry: TocEntry) -> bytes:
    """One file's content, from a bundle file and its TOC entry"""
    with open(bundle, "rb") as f:
        return read_entry(f, toc_entry).content()


def pack_entries(entries, bundle_name="blob0.bundle"):
    """(the bundle, its metadata.store) holding entries (BundleEntries), as bytes; bundle_name is the
    bundle's file name, which the store records"""
    try:
        bundle = write_bundle(entries)
    except BundleError as e:
        fail(f"can't pack the patched files: {e}")
    try:
        store = make_metadata_store([(bundle_name, bundle)])
    except MetadataStoreError as e:
        fail(f"can't make metadata.store for {bundle_name}: {e}")
    return bundle, store
