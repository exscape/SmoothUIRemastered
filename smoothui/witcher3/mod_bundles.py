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


def read_bundle_tocs(content_dirs, keys=None, extension=None) -> dict[str, tuple[Path, TocEntry]]:
    """{profile key: (bundle file, TocEntry)} for the files in the bundle(s) in content_dirs (a folder, or a
    list of them), reading only their tables of contents (see read_entry_content for a file's content). With
    keys (profile keys, or a function telling whether a key is wanted), only those files. With extension
    (default: that of every key, if they share one), only the files with it, and the other files' entries
    aren't read (see bundle.read_toc). Fails (SmoothUIError) if a bundle can't be read, or if two bundles
    hold different copies of one of the files: which one the game would use is unknown."""
    if callable(keys):
        wanted = keys
    else:
        wanted = (lambda key: True) if keys is None else keys.__contains__
        if extension is None and keys is not None:
            extensions = {os.path.splitext(key)[1] for key in keys}
            extension = (extensions.pop() or None) if len(extensions) == 1 else None
    files = {}
    for content_dir in [content_dirs] if isinstance(content_dirs, (str, Path)) else content_dirs:
        try:
            for bundle in list_bundles(content_dir):
                for entry in read_toc(bundle, extension):
                    key = profile_key(entry.path)
                    if not wanted(key):
                        continue
                    if key in files and read_entry_content(*files[key]) != read_entry_content(bundle, entry):
                        same_folder = files[key][0].parent == bundle.parent
                        names = [path.name if same_folder else path for path in (files[key][0], bundle)]
                        fail(f"{entry.path} is in both {names[0]} and {names[1]}, with different contents")
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
