"""Replace the SWF payload of a .redswf file with one of any size.

A .redswf is a REDengine 3 "CR2W" resource (version 164) holding one CSwfResource chunk (the SWF),
followed by zero or more CSwfTexture chunks. Everything is stored uncompressed, back to back:

  0      header, 40 bytes: 'CR2W' | u32 version | u32 flags | u64 timestamp | u32 build |
         u32 file_size | u32 buffer_size | u32 header_crc | u32 unknown
  40     10 table slots, 12 bytes each: u32 offset | u32 count | u32 crc32(table)
  160    the tables in use, packed in slot order:
           0  strings  count = byte length (NUL separated)
           1  names    8-byte entries: u32 string offset | u32 hash
           3  unknown  16-byte entries
           4  exports  24-byte entries: u16 class (name index) | u16 flags | u32 parent |
                       u32 size | u32 absolute offset | u32 template | u32 crc32(chunk)
  then   the chunks, contiguous and in export order, up to the end of the file. Each chunk is
         0x00 | properties | u16 0 | class specific body, where a property is
         u16 name | u16 type | u32 size (including itself) | value.
         The CSwfResource body is: u32 length | SWF file (CFX/CWS/GFX/FWS) | trailer.
         The CSwfTexture body is a 28-byte header (u32 0 | u32 1 | u32 width | u32 height |
         u32 pitch | u32 data_size | u32 16), then data_size bytes of DXT data.

The only absolute offsets are in the exports table, so a replacement rebuilds the SWF chunk, then
rewrites the exports table (offsets, sizes, CRCs) and the two size fields in the header. The header
CRC is left as is: its algorithm is unknown.
"""

from __future__ import annotations

import struct
import zlib
from typing import NamedTuple

from ..swf import SWF_COMPRESSED_SIGS, SWF_HEADER_SIZE, SWF_SIGS

CR2W_MAGIC = b"CR2W"
CR2W_VERSION = 164
OFF_FILE_SIZE, OFF_BUFFER_SIZE, OFF_HEADER_CRC = 24, 28, 32
SLOTS_START, SLOT_COUNT, SLOT_SIZE = 40, 10, 12
TABLES_START = SLOTS_START + SLOT_COUNT * SLOT_SIZE
T_STRINGS, T_NAMES, T_EXPORTS = 0, 1, 4
TABLE_ENTRY_SIZES = {T_STRINGS: 1, T_NAMES: 8, 3: 16, T_EXPORTS: 24}
OFF_EXPORTS_CRC = SLOTS_START + T_EXPORTS * SLOT_SIZE + 8
EXPORT_ROW = struct.Struct("<HHIIIII")
KNOWN_CLASSES = ("CSwfResource", "CSwfTexture")
TEXTURE_HEADER_SIZE, TEXTURE_DATA_SIZE_OFFSET = 28, 20


class RedSwfError(ValueError):
    """The .redswf or the new SWF can't be handled, or the result failed verification"""


class Export(NamedTuple):
    """A row in the exports table, describing one chunk"""
    class_idx: int
    flags: int
    parent: int
    size: int
    offset: int
    template: int
    crc: int


class RedSwf:
    """A parsed .redswf. Raises RedSwfError for anything that isn't exactly as expected."""

    def __init__(self, data: bytes | bytearray):
        self.data = d = bytes(data)
        if len(d) < TABLES_START or d[:4] != CR2W_MAGIC:
            raise RedSwfError("not a CR2W file")
        (version,) = struct.unpack_from("<I", d, 4)
        if version != CR2W_VERSION:
            raise RedSwfError(f"CR2W version {version} is not supported (expected {CR2W_VERSION})")
        self.file_size, self.buffer_size = struct.unpack_from("<II", d, OFF_FILE_SIZE)
        if self.file_size != len(d):
            raise RedSwfError(f"header file size {self.file_size} != actual size {len(d)}")

        # Tables/chunks whose stored CRC doesn't match their contents. Tolerated (it happens when
        # a SWF was patched in place), but replacing the SWF must not add any.
        self.stale = set()

        self.tables = {}   # slot: (offset, size)
        pos = TABLES_START
        for slot in range(SLOT_COUNT):
            offset, count, crc = struct.unpack_from("<III", d, SLOTS_START + slot * SLOT_SIZE)
            if count == 0:
                continue
            if slot not in TABLE_ENTRY_SIZES:
                raise RedSwfError(f"table slot {slot} is in use, but its format is unknown")
            if offset != pos:
                raise RedSwfError(f"table {slot} is at {offset}, expected {pos}")
            pos += count * TABLE_ENTRY_SIZES[slot]
            if pos > len(d):
                raise RedSwfError(f"table {slot} runs past the end of the file")
            if zlib.crc32(d[offset:pos]) != crc:
                self.stale.add(f"table {slot}")
            self.tables[slot] = (offset, pos - offset)
        for slot in (T_STRINGS, T_NAMES, T_EXPORTS):
            if slot not in self.tables:
                raise RedSwfError(f"required table {slot} is empty")
        self.tables_end = pos
        self.names = self._parse_names()

        exports_offset, exports_size = self.tables[T_EXPORTS]
        self.chunks = [Export(*EXPORT_ROW.unpack_from(d, exports_offset + i * EXPORT_ROW.size))
                       for i in range(exports_size // EXPORT_ROW.size)]
        for i, chunk in enumerate(self.chunks):
            if chunk.class_idx >= len(self.names) or self.class_name(chunk) not in KNOWN_CLASSES:
                raise RedSwfError(f"chunk {i} has an unsupported class")
            if chunk.offset != pos:
                raise RedSwfError(f"chunk {i} is at {chunk.offset}, expected {pos}")
            pos += chunk.size
            if zlib.crc32(d[chunk.offset:pos]) != chunk.crc:
                self.stale.add(f"chunk {i}")
        if pos != len(d):
            raise RedSwfError(f"the chunks end at {pos}, but the file is {len(d)} bytes")

        swf_chunks = [i for i, chunk in enumerate(self.chunks) if self.class_name(chunk) == "CSwfResource"]
        if len(swf_chunks) != 1:
            raise RedSwfError(f"expected exactly 1 CSwfResource chunk, found {len(swf_chunks)}")
        self.swf_chunk = swf_chunks[0]
        for i, chunk in enumerate(self.chunks):
            body_offset = self._skip_properties(i, chunk)
            if i == self.swf_chunk:
                self._parse_swf_body(body_offset)
            else:
                self._check_texture_body(i, chunk, body_offset)

    def class_name(self, chunk: Export) -> str:
        return self.names[chunk.class_idx]

    def chunk_bytes(self, chunk: Export) -> bytes:
        return self.data[chunk.offset:chunk.offset + chunk.size]

    def _parse_names(self) -> list[str]:
        strings_offset, strings_size = self.tables[T_STRINGS]
        strings = self.data[strings_offset:strings_offset + strings_size]
        names_offset, names_size = self.tables[T_NAMES]
        names = []
        for i in range(names_size // 8):
            (start,) = struct.unpack_from("<I", self.data, names_offset + 8 * i)
            end = strings.find(b"\0", start)
            if end < 0:
                raise RedSwfError(f"name {i} points outside the string table")
            names.append(strings[start:end].decode("latin-1"))
        return names

    def _skip_properties(self, i: int, chunk: Export) -> int:
        """Walk the chunk's property list; returns the offset of the body that follows it"""
        d = self.data
        pos, end = chunk.offset, chunk.offset + chunk.size
        if pos >= end or d[pos] != 0:
            raise RedSwfError(f"chunk {i} doesn't start with 0x00")
        pos += 1
        while True:
            if pos + 2 > end:
                raise RedSwfError(f"chunk {i}: the property list runs past the chunk")
            (name,) = struct.unpack_from("<H", d, pos)
            pos += 2
            if name == 0:
                return pos
            if pos + 6 > end or name >= len(self.names):
                raise RedSwfError(f"chunk {i}: bad property header")
            type_, size = struct.unpack_from("<HI", d, pos)
            pos += 6
            if size < 4 or type_ >= len(self.names) or pos + size - 4 > end:
                raise RedSwfError(f"chunk {i}: bad property size")
            pos += size - 4

    def _check_texture_body(self, i: int, chunk: Export, pos: int):
        """Check that the texture header and data exactly fill the chunk. Texture chunks are copied
        as is, so they must not contain anything unexplained (such as an absolute offset)."""
        end = chunk.offset + chunk.size
        if pos + TEXTURE_HEADER_SIZE > end:
            raise RedSwfError(f"texture chunk {i} is too short")
        (data_size,) = struct.unpack_from("<I", self.data, pos + TEXTURE_DATA_SIZE_OFFSET)
        if pos + TEXTURE_HEADER_SIZE + data_size != end:
            raise RedSwfError(f"texture chunk {i}: data size {data_size} doesn't match the chunk "
                              f"({end - pos - TEXTURE_HEADER_SIZE} bytes after the header)")

    def _parse_swf_body(self, pos: int):
        """Read the length-prefixed SWF and the trailer after it"""
        d, chunk = self.data, self.chunks[self.swf_chunk]
        end = chunk.offset + chunk.size
        self.swf_length_offset = pos
        if pos + 4 > end:
            raise RedSwfError("the SWF chunk is too short")
        (swf_size,) = struct.unpack_from("<I", d, pos)
        swf_offset = pos + 4
        if swf_offset + swf_size > end:
            raise RedSwfError(f"SWF length {swf_size} runs past its chunk")
        self.swf = d[swf_offset:swf_offset + swf_size]
        if self.swf[:3] not in SWF_SIGS:
            raise RedSwfError("the SWF chunk doesn't contain a SWF")
        self.swf_trailer = d[swf_offset + swf_size:end]

    def with_swf(self, swf: bytes) -> bytearray:
        """The file data with the SWF replaced; the other chunks are copied as is"""
        d = self.data
        old = self.chunks[self.swf_chunk]
        swf_chunk = d[old.offset:self.swf_length_offset] + struct.pack("<I", len(swf)) + swf + self.swf_trailer
        rows, chunks = [], []
        pos = self.tables_end
        for i, chunk in enumerate(self.chunks):
            if i == self.swf_chunk:
                chunk_data, crc = swf_chunk, zlib.crc32(swf_chunk)
            else:
                chunk_data, crc = self.chunk_bytes(chunk), chunk.crc
            rows.append(EXPORT_ROW.pack(*chunk._replace(size=len(chunk_data), offset=pos, crc=crc)))
            chunks.append(chunk_data)
            pos += len(chunk_data)

        # The exports table is the last table, and its size doesn't change
        exports = b"".join(rows)
        out = bytearray(d[:self.tables[T_EXPORTS][0]]) + exports + b"".join(chunks)
        struct.pack_into("<II", out, OFF_FILE_SIZE, len(out), self.buffer_size + len(out) - len(d))
        struct.pack_into("<I", out, OFF_EXPORTS_CRC, zlib.crc32(exports))
        return out


def replace_swf(redswf: bytes | bytearray, swf: bytes | bytearray) -> bytearray:
    """Return the .redswf file data with its SWF replaced by swf (a complete CFX/CWS/GFX/FWS file, any size).

    The result is re-parsed and checked: only the SWF and the values that depend on its size may differ.
    Raises RedSwfError if either input can't be handled, or if the result fails the checks.
    """
    swf = bytes(swf)
    check_swf(swf)
    old = RedSwf(redswf)
    if old.stale:
        print(f"[!] Stale CRCs in the original .redswf: {', '.join(sorted(old.stale))}")
    out = old.with_swf(swf)
    problems = verify_replacement(old, out, swf)
    if problems:
        raise RedSwfError(f"replacing the SWF failed verification: {'; '.join(problems)}")
    print(f"Replaced the SWF: {len(old.swf)} -> {len(swf)} bytes, "
          f".redswf {len(old.data)} -> {len(out)} bytes")
    return out


def check_swf(swf: bytes):
    """Sanity check a SWF before storing it: signature, and that it's at least as long as its header
    states (for CFX/CWS, that the zlib stream is complete and expands to at least that length)"""
    if len(swf) < SWF_HEADER_SIZE or swf[:3] not in SWF_SIGS:
        raise RedSwfError("the new SWF has no SWF signature")
    (declared,) = struct.unpack_from("<I", swf, 4)
    if swf[:3] in SWF_COMPRESSED_SIGS:
        decompressor = zlib.decompressobj()
        try:
            length = SWF_HEADER_SIZE + len(decompressor.decompress(swf[SWF_HEADER_SIZE:]))
        except zlib.error as e:
            raise RedSwfError(f"the new SWF's zlib stream is corrupt: {e}") from e
        if not decompressor.eof:
            raise RedSwfError("the new SWF's zlib stream is truncated")
    else:
        length = len(swf)
    if length < declared:
        raise RedSwfError(f"the new SWF is {length} bytes, but its header states {declared}")


def verify_replacement(old: RedSwf, out: bytearray, swf: bytes) -> list[str]:
    """Re-parse the result of old.with_swf(swf), and check that everything except the SWF and the
    values that depend on its size is unchanged. Returns a list of problems; empty if all is well."""
    try:
        new = RedSwf(out)
    except RedSwfError as e:
        return [f"the result doesn't parse: {e}"]

    problems = []
    if not new.stale <= old.stale:
        problems.append(f"new CRC mismatches: {', '.join(sorted(new.stale - old.stale))}")
    if new.swf != swf:
        problems.append("the stored SWF differs from the new one")
    if len(new.chunks) != len(old.chunks):
        return problems + ["chunk count changed"]
    for i, (a, b) in enumerate(zip(old.chunks, new.chunks)):
        if (a.class_idx, a.flags, a.parent, a.template) != (b.class_idx, b.flags, b.parent, b.template):
            problems.append(f"chunk {i} metadata changed")
        elif i != old.swf_chunk and (old.chunk_bytes(a) != new.chunk_bytes(b) or a.crc != b.crc):
            problems.append(f"chunk {i} ({old.class_name(a)}) changed")

    a, b = old.chunks[old.swf_chunk], new.chunks[new.swf_chunk]
    if old.data[a.offset:old.swf_length_offset] != new.data[b.offset:new.swf_length_offset]:
        problems.append("the SWF chunk's properties changed")
    if old.swf_trailer != new.swf_trailer:
        problems.append("the SWF chunk's trailer changed")
    if new.buffer_size - old.buffer_size != len(new.data) - len(old.data):
        problems.append("the header's buffer size is wrong")
    if old.data[TABLES_START:old.tables[T_EXPORTS][0]] != new.data[TABLES_START:new.tables[T_EXPORTS][0]]:
        problems.append("the string/name tables changed")

    allowed = {*range(OFF_FILE_SIZE, OFF_HEADER_CRC), *range(OFF_EXPORTS_CRC, OFF_EXPORTS_CRC + 4)}
    changed = {i for i in range(TABLES_START) if old.data[i] != new.data[i]}
    if changed - allowed:
        problems.append(f"unexpected header bytes changed: {sorted(changed - allowed)[:8]}")
    return problems
