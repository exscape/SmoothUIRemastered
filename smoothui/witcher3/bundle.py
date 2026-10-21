"""Read and write Witcher 3 .bundle files, without wcc_lite.

Layout (all integers little-endian):

  0x00  header, 32 bytes: 'POTATO70' | u64 bundle size | u32 TOC size (304 per file) |
        u16 version 5 | u32 data start (0x20 + TOC size) | 6 zero bytes
        The bundle size is the file size rounded up to 16 (wcc_lite doesn't pad the file itself; the
        game's bundles are padded). Sizes and offsets are 64-bit: the game's buffers.bundle and
        movies.bundle are over 4 GiB.
  0x20  the TOC, one 304-byte entry per file:
          path, 256 bytes: backslash-separated (gameplay\\gui_new\\...), NUL-terminated, zero-padded
          hash, 16 bytes (unknown; wcc_lite writes zeros) | u64 offset | u32 size |
          u32 stored size | u32 crc32 of the content | u32 compression | 8 zero bytes
        The order doesn't matter: the game's bundles are mostly unsorted, and wcc_lite writes the
        files in Windows directory listing order.
  then  the stored data of each file, at 16-byte aligned offsets, zero-padded (and to at least
        MIN_DATA_SIZE bytes)

Version 3 bundles (older wcc_lite; the game still reads them, and mods still ship them) have 320-byte
TOC entries: path | hash | u32 0 | u32 size | u32 stored size | u32 offset | u64 timestamp |
16 zero bytes | u32 crc32 | u32 compression, and their data starts at a 4096-byte boundary. These are
read, but bundles are always written as version 5.

Compression: 0 none, 1 zlib (a zlib stream), 2 snappy, 3 doboz, 4 LZ4, 5 LZ4HC (both raw LZ4 blocks).
The game's bundles use none and zlib. wcc_lite uses LZ4HC, or none when that isn't smaller, and so
does write_bundle: LZ4 decompresses several times faster than zlib, which matters when menus load.
wcc_lite skips empty files; write_bundle refuses them.

read_bundle reads a whole bundle in memory. read_toc reads only a bundle file's header and TOC (paths, sizes,
CRCs, where each file's data is), and read_entry then reads single files from it. Asked for the files of one
type only, read_toc finds them by searching the TOC, and parses only their entries: mods can have tens of
thousands of files, and parsing each entry is what takes the time.

A mod's metadata.store repeats the offset, sizes and compression of every file in its bundles, so
it must be made again whenever a bundle changes (see metadata_store.py).
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO, NamedTuple

import lz4.block

MAGIC = b"POTATO70"
HEADER = struct.Struct("<8sQIHI6x")
VERSION = 5
ENTRY = struct.Struct("<256s16sQIIII8x")
ENTRY_V3 = struct.Struct("<256s16sIIIIQ16xII")
PATH_SIZE = 256
ALIGNMENT = 16
# wcc_lite rejects a bundle with too little data after its TOC ("too small to contain headers"; the
# game likely has the same check): seen with up to 224 bytes of data, while 544 and more worked. So
# write_bundle pads small bundles with zeros up to this much data.
MIN_DATA_SIZE = 1024

NONE, ZLIB, SNAPPY, DOBOZ, LZ4, LZ4HC = range(6)
COMPRESSION_NAMES = {NONE: "none", ZLIB: "zlib", SNAPPY: "snappy", DOBOZ: "doboz", LZ4: "lz4", LZ4HC: "lz4hc"}
# Compresses in about 60% of the time of level 9, and costs less than 1% of extra space
LZ4HC_LEVEL = 6


class BundleError(ValueError):
    """A bundle that can't be read, or files that can't be put in one"""


class BundleEntry(NamedTuple):
    """One file in a bundle"""
    path: str           # as stored: backslash-separated, e.g. gameplay\gui_new\swf\gwint\gwint_game.redswf
    size: int           # of the content
    crc: int            # crc32 of the content
    compression: int
    stored: bytes       # the content as stored in the bundle (compressed or not)
    offset: int = 0     # where stored starts in the bundle (set by read_bundle; write_bundle ignores it)

    def content(self) -> bytes:
        """The file's content, decompressed and checked against its size and CRC"""
        try:
            if self.compression == NONE:
                content = self.stored
            elif self.compression == ZLIB:
                content = zlib.decompress(self.stored)
            elif self.compression in (LZ4, LZ4HC):
                content = lz4.block.decompress(self.stored, uncompressed_size=self.size)
            else:
                raise BundleError(f"{self.path}: unsupported compression: {compression_name(self.compression)}")
        except (zlib.error, lz4.block.LZ4BlockError) as e:
            raise BundleError(f"{self.path}: can't decompress ({compression_name(self.compression)}): {e}") from None
        if len(content) != self.size:
            raise BundleError(f"{self.path}: decompressed to {len(content)} bytes, expected {self.size}")
        if zlib.crc32(content) != self.crc:
            raise BundleError(f"{self.path}: CRC mismatch, the bundle is corrupt")
        return content


def compression_name(compression: int) -> str:
    return COMPRESSION_NAMES.get(compression, f"unknown ({compression})")


def depot_path(path: str | Path) -> str:
    """A relative path (with / or \\ separators) as a bundle path; raises BundleError for anything that
    can't safely be one"""
    text = str(path).replace("/", "\\")
    parts = text.split("\\")
    if any(part in ("", ".", "..") for part in parts) or ":" in text:
        raise BundleError(f"not a relative path inside the game's depot: {path}")
    if not all(32 <= ord(c) < 127 for c in text):
        raise BundleError(f"path has characters other than printable ASCII: {path}")
    if len(text) >= PATH_SIZE:
        raise BundleError(f"path is longer than {PATH_SIZE - 1} characters: {path}")
    return text


class BundleHeader(NamedTuple):
    size: int           # the bundle size in the header (which may be past the end of the file)
    data_start: int     # where the TOC ends
    version: int


class TocEntry(NamedTuple):
    """One file in a bundle's TOC: what read_toc returns, without the file's data"""
    path: str           # as stored (see BundleEntry)
    size: int           # of the content
    crc: int            # crc32 of the content
    compression: int
    offset: int         # where its stored data starts in the bundle
    stored_size: int


def read_header(data: bytes, file_size: int | None = None) -> BundleHeader:
    """A bundle's header. data holds at least the header; file_size is the bundle's size (default: len(data))."""
    file_size = len(data) if file_size is None else file_size
    if len(data) < HEADER.size or not data.startswith(MAGIC):
        raise BundleError(f"not a Witcher 3 bundle (no {MAGIC.decode()} header)")
    _, size, toc_size, version, _ = HEADER.unpack_from(data)
    if version not in (VERSION, 3):
        raise BundleError(f"unsupported bundle version {version}")
    data_start = HEADER.size + toc_size
    if toc_size % (ENTRY if version == VERSION else ENTRY_V3).size or data_start > file_size:
        raise BundleError(f"invalid TOC size {toc_size} (file size {file_size})")
    return BundleHeader(size, data_start, version)


def _parse_toc(data: bytes, header: BundleHeader, file_size: int, offsets=None) -> list[TocEntry]:
    """The TOC entries in data (the bundle's start, up to at least header.data_start), in TOC order; with
    offsets, only the entries at those offsets in data"""
    entries = []
    if offsets is None:
        offsets = range(HEADER.size, header.data_start, (ENTRY if header.version == VERSION else ENTRY_V3).size)
    for at in offsets:
        if header.version == VERSION:
            raw_path, _hash, offset, size, stored_size, crc, compression = ENTRY.unpack_from(data, at)
        else:
            raw_path, _hash, _, size, stored_size, offset, _time, crc, compression = ENTRY_V3.unpack_from(data, at)
        name = raw_path.split(b"\0", 1)[0]
        if len(name) == PATH_SIZE:
            raise BundleError(f"TOC entry at {at:#x}: path is not NUL-terminated")
        try:
            # Nearly always ASCII, but the game's own bob.bundle has a UTF-8 path (reginald’s.w2mesh)
            path = name.decode("utf-8")
        except UnicodeDecodeError:
            raise BundleError(f"TOC entry at {at:#x}: path is not UTF-8: {name!r}") from None
        if offset < header.data_start or offset + stored_size > file_size:
            raise BundleError(f"{path}: data at {offset:#x}+{stored_size:#x} is outside the bundle's data "
                              f"({header.data_start:#x}-{file_size:#x})")
        entries.append(TocEntry(path, size, crc, compression, offset, stored_size))
    return entries


def read_bundle(data: bytes) -> list[BundleEntry]:
    """The files in a bundle, in TOC order. Contents are checked when read (BundleEntry.content)."""
    return [BundleEntry(e.path, e.size, e.crc, e.compression, bytes(data[e.offset:e.offset + e.stored_size]), e.offset)
            for e in _parse_toc(data, read_header(data), len(data))]


def read_toc(path: Path, extension: str | None = None) -> list[TocEntry]:
    """The files in a bundle file, in TOC order, reading only its header and TOC: fast even for big
    texture mods. read_entry then reads the files needed. With extension (e.g. ".redswf"), only the files
    whose paths end with it, in any case; the other entries aren't checked."""
    with open(path, "rb") as f:
        file_size = f.seek(0, os.SEEK_END)
        f.seek(0)
        data = f.read(HEADER.size)
        header = read_header(data, file_size)
        data += f.read(header.data_start - len(data))
    if len(data) < header.data_start:
        raise BundleError(f"{path}: the file ended inside its TOC")
    try:
        if extension is None:
            return _parse_toc(data, header, file_size)
        entries = _parse_toc(data, header, file_size, _entries_ending(data, header, extension))
    except BundleError as e:
        raise BundleError(f"{path}: {e}") from None
    return [entry for entry in entries if entry.path.lower().endswith(extension.lower())]


def _entries_ending(data: bytes, header: BundleHeader, extension: str) -> list[int]:
    """The offsets in data of the TOC entries whose paths may end with extension (see read_toc): those with
    extension and a NUL in their path field. The caller checks each path."""
    size = (ENTRY if header.version == VERSION else ENTRY_V3).size
    toc = data[HEADER.size:header.data_start].lower()
    needle = extension.lower().encode() + b"\0"
    offsets = []
    found = toc.find(needle)
    while found != -1:
        at = HEADER.size + found - found % size
        if found % size + len(needle) <= PATH_SIZE and (not offsets or offsets[-1] != at):
            offsets.append(at)
        found = toc.find(needle, found + 1)
    return offsets


def read_entry(f: BinaryIO, entry: TocEntry) -> BundleEntry:
    """One file of a bundle open for reading in binary mode, by its TOC entry (see read_toc). Its content is
    checked when read (BundleEntry.content)."""
    f.seek(entry.offset)
    stored = f.read(entry.stored_size)
    if len(stored) != entry.stored_size:
        raise BundleError(f"{entry.path}: the bundle ended inside its data")
    return BundleEntry(entry.path, entry.size, entry.crc, entry.compression, stored, entry.offset)


def make_entry(path: str | Path, content: bytes) -> BundleEntry:
    """A file to put in a bundle: path relative to the depot (lowercase, like all the game's paths),
    content compressed with LZ4HC, or stored as is when that isn't smaller"""
    path = depot_path(path)
    if path != path.lower():
        raise BundleError(f"path is not lowercase: {path}")
    if not content:
        raise BundleError(f"{path}: file is empty (wcc_lite would leave it out of the bundle)")
    content = bytes(content)
    compressed = lz4.block.compress(content, mode="high_compression", compression=LZ4HC_LEVEL, store_size=False)
    if len(compressed) < len(content):
        entry = BundleEntry(path, len(content), zlib.crc32(content), LZ4HC, compressed)
    else:
        entry = BundleEntry(path, len(content), zlib.crc32(content), NONE, content)
    if entry.content() != content:
        raise BundleError(f"{path}: LZ4HC round trip changed the content")
    return entry


def _align(n: int) -> int:
    return -n % ALIGNMENT


def write_bundle(entries: Iterable[BundleEntry]) -> bytes:
    """A bundle holding these files, sorted by path. The result is read back and every file checked
    before it is returned, so a bundle that comes out of here is one read_bundle reads correctly."""
    entries = sorted(entries, key=lambda e: e.path)
    seen = {}
    for entry in entries:
        key = entry.path.lower()    # the depot is case-insensitive
        if key in seen:
            raise BundleError(f"file added twice: {seen[key]} and {entry.path}")
        seen[key] = entry.path
    data_start = HEADER.size + len(entries) * ENTRY.size
    out = bytearray(data_start)
    for i, entry in enumerate(entries):
        out += bytes(_align(len(out)))
        ENTRY.pack_into(out, HEADER.size + i * ENTRY.size, entry.path.encode("ascii"), bytes(16), len(out),
                        entry.size, len(entry.stored), entry.crc, entry.compression)
        out += entry.stored
    out += bytes(_align(len(out)))
    out += bytes(max(0, data_start + MIN_DATA_SIZE - len(out)))
    HEADER.pack_into(out, 0, MAGIC, len(out), data_start - HEADER.size, VERSION, data_start)
    data = bytes(out)

    written = read_bundle(data)
    if [e._replace(offset=0) for e in written] != [e._replace(offset=0) for e in entries]:
        raise BundleError("the written bundle doesn't read back as the files given")
    for entry in written:
        entry.content()
    return data


def extract(entries: Iterable[BundleEntry], out_dir: Path) -> list[Path]:
    """Write the files' contents under out_dir, at their paths. Returns the files written."""
    out_dir = out_dir.resolve()
    written = []
    for entry in entries:
        target = out_dir.joinpath(*depot_path(entry.path).split("\\"))
        if out_dir not in target.resolve().parents:
            raise BundleError(f"{entry.path}: outside the output directory")
        content = entry.content()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(target)
    return written
