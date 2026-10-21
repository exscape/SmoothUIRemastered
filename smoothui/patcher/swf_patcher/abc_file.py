"""ActionScript 3 bytecode files (the "ABC" inside a DoABC tag): parsing, and splicing changes back in.

Only the parts the patcher reads are kept; everything else is skipped over. Byte positions are
recorded for the constant pools that can be extended (doubles, strings, namespaces, multinames) and
the method bodies, so that changed versions can be spliced into the original bytes with everything
else left untouched.

Reference: the "ActionScript Virtual Machine 2 (AVM2) Overview", chapter 4.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Multiname kinds
QNAME, QNAME_A = 0x07, 0x0D
RTQNAME, RTQNAME_A = 0x0F, 0x10
RTQNAME_L, RTQNAME_LA = 0x11, 0x12
MULTINAME, MULTINAME_A = 0x09, 0x0E
MULTINAME_L, MULTINAME_LA = 0x1B, 0x1C
TYPENAME = 0x1D

# How many values a property access pops from the stack at runtime for its name, per multiname kind
RUNTIME_NAME_OPERANDS = {RTQNAME: 1, RTQNAME_A: 1, RTQNAME_L: 2, RTQNAME_LA: 2, MULTINAME_L: 1, MULTINAME_LA: 1}

# method_info flags
METHOD_HAS_OPTIONAL = 0x08
METHOD_HAS_PARAM_NAMES = 0x80

# instance_info flags
INSTANCE_PROTECTED_NS = 0x08

# Namespace kinds
PACKAGE_NAMESPACE = 0x16    # a package's public namespace; "" is the top level ("public")

# Trait kinds, and the trait attribute bit for attached metadata
TRAIT_SLOT, TRAIT_METHOD, TRAIT_GETTER, TRAIT_SETTER, TRAIT_CLASS, TRAIT_FUNCTION, TRAIT_CONST = range(7)
METHOD_TRAIT_KINDS = (TRAIT_METHOD, TRAIT_GETTER, TRAIT_SETTER)
VALUE_TRAIT_KINDS = (TRAIT_SLOT, TRAIT_CONST)
TRAIT_ATTR_METADATA = 0x4

# Constant kinds of a slot's default value
CONSTANT_UTF8 = 0x01
CONSTANT_INT = 0x03
CONSTANT_UINT = 0x04


class AbcReader:
    """Sequential reader for the ABC primitive types"""

    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset

    def u8(self) -> int:
        value = self.data[self.offset]
        self.offset += 1
        return value

    def u16(self) -> int:
        (value,) = struct.unpack_from("<H", self.data, self.offset)
        self.offset += 2
        return value

    def u30(self) -> int:
        value, self.offset = read_u30(self.data, self.offset)
        return value

    def s32(self) -> int:
        value = self.u30()
        return value - (1 << 32) if value & 0x80000000 else value

    def bytes(self, count: int) -> bytes:
        value = self.data[self.offset:self.offset + count]
        self.offset += count
        return value


# Readers as functions of the data and offset (faster in loops, such as disassembly's)

def read_u30(data: bytes, offset: int) -> tuple[int, int]:
    """Variable-length unsigned integer (u30/u32: 7 bits per byte, high bit = more bytes follow) at
    data[offset]; returns it and the offset after it"""
    byte = data[offset]
    if byte < 0x80:
        return byte, offset + 1
    value, shift = byte & 0x7F, 7
    while True:
        offset += 1
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value & 0xFFFFFFFF, offset + 1


def read_s24(data: bytes, offset: int) -> int:
    value = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
    return value - (1 << 24) if value & 0x800000 else value


def encode_u30(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_s24(value: int) -> bytes:
    return struct.pack("<i", value)[:3]


@dataclass(frozen=True, slots=True)
class Multiname:
    kind: int
    name: int = 0               # string index (QName, RTQName, Multiname)
    namespace: int = 0          # namespace index (QName)
    ns_set: int = 0             # namespace set index (Multiname, MultinameL)
    base_type: int = 0          # multiname index (TypeName, e.g. the Vector in Vector.<int>)
    type_params: tuple = ()     # multiname indices (TypeName)


@dataclass(slots=True)
class MethodInfo:
    param_types: list[int]      # multiname indices (0 = any)
    return_type: int


@dataclass(slots=True)
class Trait:
    name: int                   # multiname index
    kind: int
    slot_id: int = 0            # slots/consts
    type_name: int = 0          # slots/consts: multiname index of the declared type
    value_kind: int | None = None   # slots/consts: constant kind of the default value, if any
    method: int | None = None   # methods/getters/setters


@dataclass(slots=True)
class InstanceInfo:
    name: int                   # multiname index
    super_name: int             # multiname index, 0 if none
    constructor: int            # method index ("iinit")
    traits: list[Trait]


@dataclass(slots=True)
class ClassInfo:
    static_init: int            # method index ("cinit")
    traits: list[Trait]


@dataclass(slots=True)
class MethodBody:
    method: int
    code: bytes
    exceptions: list[list[int]]   # [from, to, target, exception type, variable name]
    traits: list[Trait]           # the slots of its activation object, if it has one
    # The code length field, code, and exception table occupy raw[splice_start:splice_end]
    splice_start: int
    splice_end: int


@dataclass(frozen=True, slots=True)
class PoolSpan:
    """Where a constant pool lives in AbcFile.raw: its count at start, its entries from values_start
    to end. Entry 0 is implicit, so a pool with n entries stores the count n + 1 (or 0 if it's empty)."""
    start: int = 0
    values_start: int = 0
    end: int = 0


@dataclass(slots=True)
class AbcFile:
    raw: bytes
    ints: list[int]
    uints: list[int]
    doubles: list[float]
    strings: list[str]
    namespaces: list[tuple[int, int]]   # (kind, name string index)
    multinames: list[Multiname | None]
    methods: list[MethodInfo]
    instances: list[InstanceInfo]
    classes: list[ClassInfo]
    bodies: dict[int, MethodBody]
    # Where the double pool (its count, then the values) lives in raw, so it can be extended
    double_pool_start: int = 0
    double_values_start: int = 0
    double_pool_end: int = 0
    double_count: int = 0           # the stored count; entry 0 is implicit, so this is len(doubles) when > 0
    # Where the string, namespace and multiname pools live in raw, so they can be extended
    string_pool: PoolSpan = PoolSpan()
    namespace_pool: PoolSpan = PoolSpan()
    multiname_pool: PoolSpan = PoolSpan()

    def name_of(self, index: int) -> str:
        """The unqualified name of a multiname; "<rt>"/"<rtns>" for names only known at runtime"""
        if index == 0:
            return "*"
        mn = self.multinames[index]
        if mn.kind in (QNAME, QNAME_A, RTQNAME, RTQNAME_A, MULTINAME, MULTINAME_A):
            return self.strings[mn.name]
        if mn.kind in (RTQNAME_L, RTQNAME_LA):
            return "<rt>"
        if mn.kind in (MULTINAME_L, MULTINAME_LA):
            return "<rtns>"
        if mn.kind == TYPENAME:
            return self.name_of(mn.base_type)
        return "?"

    def full_name_of(self, index: int) -> str:
        """Dotted name including the package, matching SymbolClass naming (pkg.Class)"""
        if index == 0:
            return "*"
        mn = self.multinames[index]
        if mn.kind in (QNAME, QNAME_A):
            ns_name_index = self.namespaces[mn.namespace][1]
            package = self.strings[ns_name_index] if ns_name_index else ""
            return f"{package}.{self.strings[mn.name]}" if package else self.strings[mn.name]
        return self.name_of(index)

    def runtime_name_operands(self, index: int) -> int:
        """Number of extra stack values an instruction using this multiname pops at runtime"""
        if index == 0:
            return 0
        return RUNTIME_NAME_OPERANDS.get(self.multinames[index].kind, 0)


def parse_abc(data: bytes) -> AbcFile:
    r = AbcReader(data)
    r.u16(), r.u16()    # minor, major version

    def pool(read_entry):
        """Constant pools store count+1, and entry 0 is implicit"""
        return [read_entry() for _ in range(max(r.u30() - 1, 0))]

    def located_pool(read_entry):
        """pool(read_entry), and its PoolSpan"""
        start = r.offset
        count = r.u30()
        values_start = r.offset
        entries = [read_entry() for _ in range(max(count - 1, 0))]
        return entries, PoolSpan(start, values_start, r.offset)

    ints = [0] + pool(r.s32)
    uints = [0] + pool(r.u30)

    double_pool_start = r.offset
    double_count = r.u30()
    double_values_start = r.offset
    doubles = [0.0] + [struct.unpack("<d", r.bytes(8))[0] for _ in range(max(double_count - 1, 0))]
    double_pool_end = r.offset

    strings, string_pool = located_pool(lambda: r.bytes(r.u30()).decode("utf8", "replace"))
    namespaces, namespace_pool = located_pool(lambda: (r.u8(), r.u30()))
    pool(lambda: [r.u30() for _ in range(r.u30())])     # namespace sets
    multinames, multiname_pool = located_pool(lambda: _read_multiname(r))
    strings, namespaces, multinames = [""] + strings, [(0, 0)] + namespaces, [None] + multinames

    methods = [_read_method_info(r) for _ in range(r.u30())]

    for _ in range(r.u30()):    # metadata: name, then key/value pairs
        r.u30()
        for _ in range(r.u30() * 2):
            r.u30()

    class_count = r.u30()
    instances = [_read_instance_info(r) for _ in range(class_count)]
    classes = [ClassInfo(r.u30(), _read_traits(r)) for _ in range(class_count)]

    for _ in range(r.u30()):    # scripts
        r.u30()
        _read_traits(r)

    bodies = {}
    for _ in range(r.u30()):
        body = _read_method_body(r)
        bodies[body.method] = body

    if r.offset != len(data):
        raise ValueError("ABC parse did not consume all bytes")

    return AbcFile(data, ints, uints, doubles, strings, namespaces, multinames, methods, instances, classes,
                   bodies, double_pool_start, double_values_start, double_pool_end, double_count,
                   string_pool, namespace_pool, multiname_pool)


def _read_multiname(r: AbcReader) -> Multiname:
    kind = r.u8()
    if kind in (QNAME, QNAME_A):
        namespace = r.u30()
        return Multiname(kind, name=r.u30(), namespace=namespace)
    if kind in (RTQNAME, RTQNAME_A):
        return Multiname(kind, name=r.u30())
    if kind in (RTQNAME_L, RTQNAME_LA):
        return Multiname(kind)
    if kind in (MULTINAME, MULTINAME_A):
        name = r.u30()
        return Multiname(kind, name=name, ns_set=r.u30())
    if kind in (MULTINAME_L, MULTINAME_LA):
        return Multiname(kind, ns_set=r.u30())
    if kind == TYPENAME:
        base_type = r.u30()
        return Multiname(kind, base_type=base_type, type_params=tuple(r.u30() for _ in range(r.u30())))
    raise ValueError(f"bad multiname kind {kind:#x}")


def _read_method_info(r: AbcReader) -> MethodInfo:
    param_count = r.u30()
    return_type = r.u30()
    param_types = [r.u30() for _ in range(param_count)]
    r.u30()     # name
    flags = r.u8()
    if flags & METHOD_HAS_OPTIONAL:
        for _ in range(r.u30()):
            r.u30()
            r.u8()
    if flags & METHOD_HAS_PARAM_NAMES:
        for _ in range(param_count):
            r.u30()
    return MethodInfo(param_types, return_type)


def _read_instance_info(r: AbcReader) -> InstanceInfo:
    name = r.u30()
    super_name = r.u30()
    flags = r.u8()
    if flags & INSTANCE_PROTECTED_NS:
        r.u30()
    for _ in range(r.u30()):    # interfaces
        r.u30()
    constructor = r.u30()
    return InstanceInfo(name, super_name, constructor, _read_traits(r))


def _read_traits(r: AbcReader) -> list[Trait]:
    traits = []
    for _ in range(r.u30()):
        name = r.u30()
        kind_byte = r.u8()
        kind, attributes = kind_byte & 0xF, kind_byte >> 4
        trait = Trait(name, kind)
        if kind in VALUE_TRAIT_KINDS:
            trait.slot_id = r.u30()
            trait.type_name = r.u30()
            if r.u30():     # default value index
                trait.value_kind = r.u8()
        elif kind in (TRAIT_CLASS, TRAIT_FUNCTION):
            r.u30()     # slot id
            r.u30()     # class / function index
        elif kind in METHOD_TRAIT_KINDS:
            r.u30()     # disp id
            trait.method = r.u30()
        else:
            raise ValueError(f"bad trait kind {kind}")
        if attributes & TRAIT_ATTR_METADATA:
            for _ in range(r.u30()):
                r.u30()
        traits.append(trait)
    return traits


def _read_method_body(r: AbcReader) -> MethodBody:
    method = r.u30()
    for _ in range(4):  # max stack, local count, init/max scope depth
        r.u30()
    splice_start = r.offset
    code = bytes(r.bytes(r.u30()))
    exceptions = [[r.u30() for _ in range(5)] for _ in range(r.u30())]
    splice_end = r.offset
    return MethodBody(method, code, exceptions, _read_traits(r), splice_start, splice_end)


@dataclass(slots=True)
class PoolAdditions:
    """Strings, namespaces and multinames to append to an ABC file's constant pools (see serialize_abc),
    for code that names things the file doesn't (yet) name. Each method returns the index to use in
    code: an existing entry's if there is an equal one, or else the one a new entry gets. Existing
    indices stay valid."""
    abc: AbcFile
    strings: list[str] = field(default_factory=list)
    namespaces: list[tuple[int, int]] = field(default_factory=list)    # (kind, name string index)
    multinames: list[Multiname] = field(default_factory=list)

    def string(self, text: str) -> int:
        return self._index(self.abc.strings, self.strings, text)

    def namespace(self, kind: int, name: str) -> int:
        return self._index(self.abc.namespaces, self.namespaces, (kind, self.string(name)))

    def qname(self, namespace: int, name: str) -> int:
        """A QName multiname: name in the namespace with that index (see namespace())"""
        return self._index(self.abc.multinames, self.multinames,
                           Multiname(QNAME, name=self.string(name), namespace=namespace))

    @staticmethod
    def _index(existing: list, added: list, entry) -> int:
        # Entry 0 is the implicit one ("any name", no namespace...), never an actual constant
        for index in range(1, len(existing)):
            if existing[index] == entry:
                return index
        if entry not in added:
            added.append(entry)
        return len(existing) + added.index(entry)


def _encode_multiname(multiname: Multiname) -> bytes:
    if multiname.kind not in (QNAME, QNAME_A):
        raise ValueError(f"can't add a multiname of kind {multiname.kind:#x}")
    return bytes([multiname.kind]) + encode_u30(multiname.namespace) + encode_u30(multiname.name)


def _extend_pool(abc: AbcFile, span: PoolSpan, existing_count: int, new_entries: list[bytes]) -> tuple[int, int, bytes]:
    """The splice that appends new_entries (encoded) to the pool at span, which has existing_count
    entries counting the implicit entry 0"""
    return (span.start, span.end,
            encode_u30(existing_count + len(new_entries)) + abc.raw[span.values_start:span.end] + b"".join(new_entries))


def serialize_abc(abc: AbcFile, new_bodies: dict[int, tuple[bytes, list]], new_doubles=(),
                  additions: PoolAdditions | None = None) -> bytes:
    """The ABC bytes with the given method bodies replaced, and doubles and other constants (additions)
    appended to their pools.

    new_bodies maps method index -> (code, exception table). Appended doubles get the indices
    following the existing pool entries; existing indices stay valid."""
    splices = []    # (start, end, replacement) in abc.raw
    if additions is not None:
        if additions.abc is not abc:
            raise ValueError("BUG: pool additions made for a different ABC file")
        if additions.strings:
            splices.append(_extend_pool(abc, abc.string_pool, len(abc.strings), [
                encode_u30(len(data)) + data for data in (text.encode("utf8") for text in additions.strings)]))
        if additions.namespaces:
            splices.append(_extend_pool(abc, abc.namespace_pool, len(abc.namespaces), [
                bytes([kind]) + encode_u30(name) for kind, name in additions.namespaces]))
        if additions.multinames:
            splices.append(_extend_pool(abc, abc.multiname_pool, len(abc.multinames),
                                        [_encode_multiname(multiname) for multiname in additions.multinames]))
    if new_doubles:
        first_new_index = max(abc.double_count, 1)
        old_values = abc.raw[abc.double_values_start:abc.double_pool_end] if abc.double_count >= 1 else b""
        new_values = b"".join(struct.pack("<d", value) for value in new_doubles)
        splices.append((abc.double_pool_start, abc.double_pool_end,
                        encode_u30(first_new_index + len(new_doubles)) + old_values + new_values))

    for method, (code, exceptions) in new_bodies.items():
        body = abc.bodies[method]
        replacement = (encode_u30(len(code)) + code + encode_u30(len(exceptions))
                       + b"".join(encode_u30(value) for entry in exceptions for value in entry))
        splices.append((body.splice_start, body.splice_end, replacement))

    out, position = [], 0
    for start, end, replacement in sorted(splices):
        out += [abc.raw[position:start], replacement]
        position = end
    out.append(abc.raw[position:])
    return b"".join(out)
