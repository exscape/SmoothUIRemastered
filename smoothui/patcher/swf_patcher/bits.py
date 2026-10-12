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
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.offset] >> (7 - self.bit)) & 1)
            self.bit += 1
            if self.bit == 8:
                self.bit = 0
                self.offset += 1
        return value

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
        self.bits: list[int] = []

    def write(self, value: int, count: int):
        for i in range(count - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def write_signed(self, value: int, count: int):
        if count:
            self.write(value & ((1 << count) - 1), count)

    def to_bytes(self) -> bytes:
        bits = self.bits + [0] * (-len(self.bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def signed_bits_needed(values) -> int:
    """Smallest signed field width that holds all the values (clamped to 1..31 bits)"""
    width = 0
    for value in values:
        width = max(width, (value if value >= 0 else ~value).bit_length() + 1)
    return min(max(width, 1), 31)
