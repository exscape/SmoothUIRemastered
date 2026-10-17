"""Read and write Witcher 3 .bundle files, without wcc_lite.

Layout (all integers little-endian):

  0x00  header, 32 bytes: 'POTATO70' | u32 bundle size | u32 0 | u32 TOC size (304 per file) |
        u16 version 5 | u32 data start (0x20 + TOC size) | 6 zero bytes
        The bundle size is the file size rounded up to 16 (wcc_lite doesn't pad the file itself; the
        game's bundles are padded). The u32 at 0x0C is 0 in all but 4 of the game's bundles (unknown).
  0x20  the TOC, one 304-byte entry per file:
          path, 256 bytes: backslash-separated (gameplay\\gui_new\\...), NUL-terminated, zero-padded
          hash, 16 bytes (unknown; wcc_lite writes zeros) | u32 offset | u32 0 | u32 size |
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

A mod's metadata.store repeats the offset, sizes and compression of every file in its bundles, so
it must be made again whenever a bundle changes (see metadata_store.py).
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

import lz4.block

MAGIC = b"POTATO70"
HEADER = struct.Struct("<8sIIIHI6x")
VERSION = 5
ENTRY = struct.Struct("<256s16sIIIIII8x")
ENTRY_V3 = struct.Struct("<256s16sIIIIQ16xII")
PATH_SIZE = 256
ALIGNMENT = 16
MAX_BUNDLE_SIZE = 2**32 - ALIGNMENT
# wcc_lite rejects a bundle with too little data after its TOC ("too small to contain headers"; the
# game likely has the same check): seen with up to 224 bytes of data, while 544 and more worked. So
# write_bundle pads small bundles with zeros up to this much data.
MIN_DATA_SIZE = 1024

NONE, ZLIB, SNAPPY, DOBOZ, LZ4, LZ4HC = range(6)
COMPRESSION_NAMES = {NONE: "none", ZLIB: "zlib", SNAPPY: "snappy", DOBOZ: "doboz", LZ4: "lz4", LZ4HC: "lz4hc"}
# LZ4's default high compression level; the output is about as small as wcc_lite's
LZ4HC_LEVEL = 9


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


def read_header(data: bytes) -> BundleHeader:
    if len(data) < HEADER.size or not data.startswith(MAGIC):
        raise BundleError(f"not a Witcher 3 bundle (no {MAGIC.decode()} header)")
    _, size, _, toc_size, version, _ = HEADER.unpack_from(data)
    if version not in (VERSION, 3):
        raise BundleError(f"unsupported bundle version {version}")
    data_start = HEADER.size + toc_size
    if toc_size % (ENTRY if version == VERSION else ENTRY_V3).size or data_start > len(data):
        raise BundleError(f"invalid TOC size {toc_size} (file size {len(data)})")
    return BundleHeader(size, data_start, version)


def read_bundle(data: bytes) -> list[BundleEntry]:
    """The files in a bundle, in TOC order. Contents are checked when read (BundleEntry.content)."""
    data_start, version = read_header(data)[1:]
    entries = []
    for at in range(HEADER.size, data_start, (ENTRY if version == VERSION else ENTRY_V3).size):
        if version == VERSION:
            raw_path, _hash, offset, _, size, stored_size, crc, compression = ENTRY.unpack_from(data, at)
        else:
            raw_path, _hash, _, size, stored_size, offset, _time, crc, compression = ENTRY_V3.unpack_from(data, at)
        name = raw_path.split(b"\0", 1)[0]
        if len(name) == PATH_SIZE:
            raise BundleError(f"TOC entry at {at:#x}: path is not NUL-terminated")
        try:
            path = name.decode("ascii")
        except UnicodeDecodeError:
            raise BundleError(f"TOC entry at {at:#x}: path is not ASCII: {name!r}") from None
        if offset < data_start or offset + stored_size > len(data):
            raise BundleError(f"{path}: data at {offset:#x}+{stored_size:#x} is outside the bundle's data "
                              f"({data_start:#x}-{len(data):#x})")
        entries.append(BundleEntry(path, size, crc, compression, bytes(data[offset:offset + stored_size]), offset))
    return entries


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
        ENTRY.pack_into(out, HEADER.size + i * ENTRY.size, entry.path.encode("ascii"), bytes(16), len(out), 0,
                        entry.size, len(entry.stored), entry.crc, entry.compression)
        out += entry.stored
    out += bytes(_align(len(out)))
    out += bytes(max(0, data_start + MIN_DATA_SIZE - len(out)))
    if len(out) > MAX_BUNDLE_SIZE:
        raise BundleError(f"bundle would be {len(out)} bytes, more than its 32-bit offsets allow")
    HEADER.pack_into(out, 0, MAGIC, len(out), 0, data_start - HEADER.size, VERSION, data_start)
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
