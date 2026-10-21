"""Bit-level reading and writing, for the packed SWF records (MATRIX, CXFORMWITHALPHA)"""

from __future__ import annotations


class BitReader:
    """Reads MSB-first bit fields, starting at a byte offset"""

    def __init__(self, data: bytes, offset: int):
        self.data = data
        self.offset = offset    # current byte
        self.bit = 0            # bits already consumed from the current byte

    def read(self, count: int) -> int:
        """Unsigned field of `count` bits"""
        end_bit = self.bit + count
        size = (end_bit + 7) >> 3    # bytes the field touches
        if self.offset + size > len(self.data):
            raise IndexError(f"bit field runs past the end of the data, at offset {self.offset}")
        chunk = int.from_bytes(self.data[self.offset:self.offset + size], "big")
        self.offset += end_bit >> 3
        self.bit = end_bit & 7
        return (chunk >> (size * 8 - end_bit)) & ((1 << count) - 1)

    def read_signed(self, count: int) -> int:
        """Two's complement field of `count` bits"""
        if count == 0:
            return 0
        value = self.read(count)
        return value - (1 << count) if value & (1 << (count - 1)) else value

    def align(self) -> int:
        """Skip to the next byte boundary; returns the new byte offset"""
        if self.bit:
            self.bit = 0
            self.offset += 1
        return self.offset


class BitWriter:
    """Builds MSB-first bit fields; the result is zero padded to a whole byte"""

    def __init__(self):
        self.value = 0      # the bits written so far, the first one highest
        self.count = 0

    def write(self, value: int, count: int):
        """The low `count` bits of value"""
        self.value = (self.value << count) | (value & ((1 << count) - 1))
        self.count += count

    def write_signed(self, value: int, count: int):
        if count:
            self.write(value, count)

    def to_bytes(self) -> bytes:
        padding = -self.count % 8
        return (self.value << padding).to_bytes((self.count + padding) // 8, "big")


def signed_bits_needed(values) -> int:
    """Smallest signed field width that holds all the values (clamped to 1..31 bits)"""
    width = 0
    for value in values:
        width = max(width, (value if value >= 0 else ~value).bit_length() + 1)
    return min(max(width, 1), 31)
