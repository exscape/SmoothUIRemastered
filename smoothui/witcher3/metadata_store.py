"""Make the metadata.store of a mod's content folder (as wcc_lite metadatastore does), and check one
against the folder's bundles. The game needs it: without one, a mod's bundles are ignored.

The store indexes the files in the bundles. Layout (version 7; all integers little-endian; "str" is an
offset into the string table; "file", "entry", "dir" and "bundle" are indexes into those arrays):

  header, 16 bytes: '\\x03VTM' | u32 7 | u32 largest stored size | u32 largest size (over all files)
  then 8 arrays, each a count followed by its records. Counts are CR2W packed ints (first byte: 0x80
  sign, 0x40 more bytes follow, 6 bits of value; then 7 bits of value and 0x80 more bytes follow), and
  the count of an empty array is written as 0x80. Index 0 of files, entries and bundles is an
  all-zero record, meaning none.

  strings    count = bytes: NUL-terminated strings: "", the bundles' names, the files' paths, ""
             (the root directory), the directories' names
  files      32 bytes: str path | u32 0 | u32 stored size | u32 size | u32 entry | u32 0 | u32 0 |
             u32 compression
  entries    24 bytes: u64 offset in the bundle | u64 stored size | u32 file | u32 bundle
  bundles    24 bytes: u64 data size (the bundle size in its header - data start) | u32 data start |
             str name | u32 first entry | u32 entry count
  buffers    always empty here (files with buffers, such as meshes, are not supported)
  dirs       8 bytes: str name | u32 parent dir; the root first, then each directory as its first
             file comes up
  file dirs  12 bytes, one per file: u32 file | u32 dir | str file name (a suffix of the path string)
  hashes     16 bytes, sorted by hash: u64 FNV-1a 64 of the lowercase path | u32 file | i32 0 for
             file 1, -1 for the others (unexplained, but that's what wcc_lite writes)

Files are in the order of their data in the bundle, not of the TOC. Verified byte for byte against
wcc_lite's stores of 22 single-bundle mods without buffers, and of test bundles (unsorted TOC,
mixed case, files in the root, repeated directory names). Several bundles in one folder, buffers and
mixed-case paths are refused: they are untested (wcc_lite rejected the test bundles of the first).
Version 6 stores (from older tools; the game still reads them) are laid out differently and aren't read.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import NamedTuple

from .bundle import BundleEntry, BundleError, read_bundle, read_header

MAGIC = b"\x03VTM"
VERSION = 7
HEADER = struct.Struct("<4sIII")
FILE = struct.Struct("<8I")
ENTRY = struct.Struct("<QQII")
BUNDLE = struct.Struct("<QIIII")
DIR = struct.Struct("<II")
FILE_DIR = struct.Struct("<III")
HASH = struct.Struct("<QIi")
# The records of the arrays after the string table, in file order
ARRAYS = (("files", FILE), ("entries", ENTRY), ("bundles", BUNDLE), ("buffers", None),
          ("dirs", DIR), ("file_dirs", FILE_DIR), ("hashes", HASH))
EMPTY_COUNT = b"\x80"


class MetadataStoreError(ValueError):
    """A metadata.store that can't be made, read, or doesn't match its bundles"""


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for byte in data:
        h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def _packed_int(n: int) -> bytes:
    if n == 0:
        return EMPTY_COUNT
    out = bytearray([n & 0x3F | (0x40 if n >> 6 else 0)])
    n >>= 6
    while n:
        out.append(n & 0x7F | (0x80 if n >> 7 else 0))
        n >>= 7
    return bytes(out)


def _read_packed_int(data: bytes, at: int) -> tuple[int, int]:
    """The packed int at data[at] (a negative number is an error) and the offset after it"""
    try:
        byte = data[at]
        negative, n, more, shift = byte & 0x80, byte & 0x3F, byte & 0x40, 6
        at += 1
        while more:
            byte = data[at]
            n |= (byte & 0x7F) << shift
            more, shift = byte & 0x80, shift + 7
            at += 1
    except IndexError:
        raise MetadataStoreError("truncated") from None
    if negative and n:
        raise MetadataStoreError(f"negative count at {at:#x}")
    return n, at


class _Strings:
    def __init__(self):
        self.data = bytearray()

    def add(self, text: str) -> int:
        offset = len(self.data)
        self.data += text.encode("ascii") + b"\0"
        return offset


def make_metadata_store(bundles: Sequence[tuple[str, bytes]]) -> bytes:
    """The metadata.store for a content folder holding these bundles: (file name, contents). The result
    is read back and checked against the bundles before it is returned."""
    if len(bundles) != 1:
        raise MetadataStoreError(f"{len(bundles)} bundles: only one bundle per content folder is supported")
    store = _make_store(bundles)
    check_metadata_store(store, bundles)
    return store


def _make_store(bundles: Sequence[tuple[str, bytes]]) -> bytes:
    try:
        contents = [(name, read_header(data), _sorted_entries(data)) for name, data in bundles]
    except BundleError as e:
        raise MetadataStoreError(f"can't read the bundle: {e}") from None
    all_entries = [e for _, _, entries in contents for e in entries]
    _check_paths(all_entries)

    strings = _Strings()
    strings.add("")
    bundle_names = [strings.add(name) for name, _, _ in contents]
    path_offsets = [strings.add(e.path) for e in all_entries]

    # Index 0 is the root directory; a directory is keyed by its path
    dir_index = {"": 0}
    dir_records = [("", 0)]      # (name, parent)
    file_dirs = []
    for file, entry in enumerate(all_entries, 1):
        parent = ""
        for name in entry.path.split("\\")[:-1]:
            key = f"{parent}\\{name}" if parent else name
            if key not in dir_index:
                dir_index[key] = len(dir_records)
                dir_records.append((name, dir_index[parent]))
            parent = key
        name_offset = path_offsets[file - 1] + (len(parent) + 1 if parent else 0)
        file_dirs.append(FILE_DIR.pack(file, dir_index[parent], name_offset))
    dirs = [DIR.pack(strings.add(name), parent) for name, parent in dir_records]

    files = [bytes(FILE.size)]
    entries = [bytes(ENTRY.size)]
    bundle_records = [bytes(BUNDLE.size)]
    for bundle, ((name, header, bundle_entries), name_offset) in enumerate(zip(contents, bundle_names), 1):
        bundle_records.append(BUNDLE.pack(header.size - header.data_start, header.data_start, name_offset,
                                          len(entries), len(bundle_entries)))
        for e in bundle_entries:
            file = len(files)
            files.append(FILE.pack(path_offsets[file - 1], 0, len(e.stored), e.size, len(entries), 0, 0, e.compression))
            entries.append(ENTRY.pack(e.offset, len(e.stored), file, bundle))
    hashes = sorted((fnv1a64(e.path.lower().encode("ascii")), file) for file, e in enumerate(all_entries, 1))

    out = bytearray(HEADER.pack(MAGIC, VERSION, max((len(e.stored) for e in all_entries), default=0),
                                max((e.size for e in all_entries), default=0)))
    out += _packed_int(len(strings.data)) + strings.data
    arrays = {"files": files, "entries": entries, "bundles": bundle_records, "buffers": [], "dirs": dirs,
              "file_dirs": file_dirs, "hashes": [HASH.pack(h, file, 0 if file == 1 else -1) for h, file in hashes]}
    for name, _ in ARRAYS:
        out += _packed_int(len(arrays[name])) + b"".join(arrays[name])
    return bytes(out)


def _sorted_entries(bundle: bytes) -> list[BundleEntry]:
    """A bundle's files in the order its store lists them: by where their data is (whatever the order
    of the TOC)"""
    return sorted(read_bundle(bundle), key=lambda e: e.offset)


def _check_paths(entries: list[BundleEntry]):
    seen = set()
    for e in entries:
        if e.path != e.path.lower():
            raise MetadataStoreError(f"{e.path}: not lowercase (untested)")
        if e.path.endswith(".buffer"):
            raise MetadataStoreError(f"{e.path}: buffers are not supported")
        if e.path in seen:
            raise MetadataStoreError(f"{e.path}: in the bundles twice")
        seen.add(e.path)


class StoreFile(NamedTuple):
    """One file, as the store describes it"""
    path: str
    name: str           # the file name, as the file dirs array has it
    dir_path: str       # the path of its directory, from the dirs array ("" for the root)
    size: int
    stored_size: int
    compression: int
    offset: int
    bundle: str


class StoreBundle(NamedTuple):
    name: str
    data_size: int
    data_start: int


class MetadataStore(NamedTuple):
    files: list[StoreFile]
    bundles: list[StoreBundle]


def read_metadata_store(data: bytes) -> MetadataStore:
    """What a (version 7) metadata.store describes, in its order. Raises MetadataStoreError for
    anything it doesn't expect, including files with buffers."""
    if len(data) < HEADER.size:
        raise MetadataStoreError("truncated")
    magic, version, max_stored, max_size = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise MetadataStoreError("not a metadata.store")
    if version != VERSION:
        raise MetadataStoreError(f"version {version}; only version {VERSION} is supported")
    size, at = _read_packed_int(data, HEADER.size)
    strings = data[at:at + size]
    at += size
    arrays = {}
    for name, record in ARRAYS:
        count, at = _read_packed_int(data, at)
        if record is None:
            if count:
                raise MetadataStoreError(f"{count} {name}: not supported")
            continue
        end = at + count * record.size
        if end > len(data):
            raise MetadataStoreError("truncated")
        arrays[name] = [record.unpack_from(data, at + k * record.size) for k in range(count)]
        at = end
    if at != len(data):
        raise MetadataStoreError(f"{len(data) - at} bytes after the last array")

    def string(offset):
        end = strings.find(b"\0", offset)
        if offset >= len(strings) or end < 0:
            raise MetadataStoreError(f"string offset {offset} is outside the string table")
        return strings[offset:end].decode("ascii")

    def get(name, index):
        if not 0 < index < len(arrays[name]):
            raise MetadataStoreError(f"{name} index {index} is out of range")
        return arrays[name][index]

    def dir_path(index):
        parts = []
        for _ in range(len(arrays["dirs"])):
            if index == 0:
                return "\\".join(reversed(parts))
            name, index = get("dirs", index)
            parts.append(string(name))
        raise MetadataStoreError("loop in the directory tree")

    for name in ("files", "entries", "bundles"):
        if not arrays[name] or any(arrays[name][0]):
            raise MetadataStoreError(f"{name} doesn't start with an empty record")
    if not arrays["dirs"] or arrays["dirs"][0][1] != 0 or string(arrays["dirs"][0][0]) != "":
        raise MetadataStoreError("dirs doesn't start with the root")
    file_count = len(arrays["files"]) - 1
    file_dirs = {file: (d, name) for file, d, name in arrays["file_dirs"]}
    if len(arrays["file_dirs"]) != file_count or sorted(file_dirs) != list(range(1, file_count + 1)):
        raise MetadataStoreError("file dirs don't list every file once")
    hashes = arrays["hashes"]
    if sorted(file for _, file, _ in hashes) != list(range(1, file_count + 1)):
        raise MetadataStoreError("hashes don't list every file once")
    if [h for h, _, _ in hashes] != sorted(h for h, _, _ in hashes):
        raise MetadataStoreError("hashes are not sorted")
    for bundle, (_, _, _, first, count) in enumerate(arrays["bundles"][1:], 1):
        for entry in range(first, first + count):
            if get("entries", entry)[3] != bundle:
                raise MetadataStoreError(f"entry {entry} is not in bundle {bundle}, which lists it")

    files = []
    for file, (path, _, stored_size, size, entry, _, _, compression) in enumerate(arrays["files"][1:], 1):
        path = string(path)
        offset, entry_stored_size, entry_file, bundle = get("entries", entry)
        if entry_file != file or entry_stored_size != stored_size:
            raise MetadataStoreError(f"{path}: entry {entry} doesn't match the file")
        d, name = file_dirs[file]
        files.append(StoreFile(path, string(name), dir_path(d), size, stored_size, compression, offset,
                               string(get("bundles", bundle)[2])))
    for h, file, tail in hashes:
        if h != fnv1a64(files[file - 1].path.lower().encode("ascii")) or tail != (0 if file == 1 else -1):
            raise MetadataStoreError(f"{files[file - 1].path}: wrong hash record")
    if max_stored != max((f.stored_size for f in files), default=0) or max_size != max((f.size for f in files), default=0):
        raise MetadataStoreError("largest sizes in the header are wrong")
    bundles = [StoreBundle(string(name), data_size, data_start)
               for data_size, data_start, name, _, _ in arrays["bundles"][1:]]
    return MetadataStore(files, bundles)


def check_metadata_store(store: bytes, bundles: Sequence[tuple[str, bytes]]):
    """Raise MetadataStoreError unless the store describes exactly the files in these bundles:
    (file name, contents), with the right paths, directories, offsets, sizes and compression"""
    expected = MetadataStore([], [])
    try:
        for name, data in bundles:
            header = read_header(data)
            expected.bundles.append(StoreBundle(name, header.size - header.data_start, header.data_start))
            for e in _sorted_entries(data):
                directory, _, file_name = e.path.rpartition("\\")
                expected.files.append(StoreFile(e.path, file_name, directory, e.size, len(e.stored), e.compression,
                                                e.offset, name))
    except BundleError as e:
        raise MetadataStoreError(f"can't read the bundle: {e}") from None
    actual = read_metadata_store(store)
    for what in ("bundles", "files"):
        have, want = getattr(actual, what), getattr(expected, what)
        if have != want:
            have, want = next((h, w) for h, w in zip(have + [None] * len(want), want + [None] * len(have)) if h != w)
            raise MetadataStoreError(f"the store doesn't match the bundles: it has {have}, the bundles {want}")
