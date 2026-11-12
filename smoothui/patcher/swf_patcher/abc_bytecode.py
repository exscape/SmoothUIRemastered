"""AVM2 method bytecode: disassembly, re-assembly with replaced constants, and a small stack model"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .abc_file import AbcFile, encode_s24, encode_u30, read_s24, read_u30


class Op(IntEnum):
    """The AVM2 opcodes. A member's name in lower case is the instruction's name. The ALCHEMY ones are Alchemy's
    memory instructions."""
    BKPT = 0x01
    NOP = 0x02
    THROW = 0x03
    GETSUPER = 0x04
    SETSUPER = 0x05
    DXNS = 0x06
    DXNSLATE = 0x07
    KILL = 0x08
    LABEL = 0x09
    IFNLT = 0x0C
    IFNLE = 0x0D
    IFNGT = 0x0E
    IFNGE = 0x0F
    JUMP = 0x10
    IFTRUE = 0x11
    IFFALSE = 0x12
    IFEQ = 0x13
    IFNE = 0x14
    IFLT = 0x15
    IFLE = 0x16
    IFGT = 0x17
    IFGE = 0x18
    IFSTRICTEQ = 0x19
    IFSTRICTNE = 0x1A
    LOOKUPSWITCH = 0x1B
    PUSHWITH = 0x1C
    POPSCOPE = 0x1D
    NEXTNAME = 0x1E
    HASNEXT = 0x1F
    PUSHNULL = 0x20
    PUSHUNDEFINED = 0x21
    NEXTVALUE = 0x23
    PUSHBYTE = 0x24
    PUSHSHORT = 0x25
    PUSHTRUE = 0x26
    PUSHFALSE = 0x27
    PUSHNAN = 0x28
    POP = 0x29
    DUP = 0x2A
    SWAP = 0x2B
    PUSHSTRING = 0x2C
    PUSHINT = 0x2D
    PUSHUINT = 0x2E
    PUSHDOUBLE = 0x2F
    PUSHSCOPE = 0x30
    PUSHNAMESPACE = 0x31
    HASNEXT2 = 0x32
    ALCHEMY35 = 0x35
    ALCHEMY36 = 0x36
    ALCHEMY37 = 0x37
    ALCHEMY38 = 0x38
    ALCHEMY39 = 0x39
    ALCHEMY3A = 0x3A
    ALCHEMY3B = 0x3B
    ALCHEMY3C = 0x3C
    ALCHEMY3D = 0x3D
    ALCHEMY3E = 0x3E
    ALCHEMY3F = 0x3F
    NEWFUNCTION = 0x40
    CALL = 0x41
    CONSTRUCT = 0x42
    CALLMETHOD = 0x43
    CALLSTATIC = 0x44
    CALLSUPER = 0x45
    CALLPROPERTY = 0x46
    RETURNVOID = 0x47
    RETURNVALUE = 0x48
    CONSTRUCTSUPER = 0x49
    CONSTRUCTPROP = 0x4A
    CALLPROPLEX = 0x4C
    CALLSUPERVOID = 0x4E
    CALLPROPVOID = 0x4F
    APPLYTYPE = 0x53
    NEWOBJECT = 0x55
    NEWARRAY = 0x56
    NEWACTIVATION = 0x57
    NEWCLASS = 0x58
    GETDESCENDANTS = 0x59
    NEWCATCH = 0x5A
    FINDPROPSTRICT = 0x5D
    FINDPROPERTY = 0x5E
    FINDDEF = 0x5F
    GETLEX = 0x60
    SETPROPERTY = 0x61
    GETLOCAL = 0x62
    SETLOCAL = 0x63
    GETGLOBALSCOPE = 0x64
    GETSCOPEOBJECT = 0x65
    GETPROPERTY = 0x66
    INITPROPERTY = 0x68
    DELETEPROPERTY = 0x6A
    GETSLOT = 0x6C
    SETSLOT = 0x6D
    GETGLOBALSLOT = 0x6E
    SETGLOBALSLOT = 0x6F
    CONVERT_S = 0x70
    ESC_XELEM = 0x71
    ESC_XATTR = 0x72
    CONVERT_I = 0x73
    CONVERT_U = 0x74
    CONVERT_D = 0x75
    CONVERT_B = 0x76
    CONVERT_O = 0x77
    CHECKFILTER = 0x78
    COERCE = 0x80
    COERCE_A = 0x82
    COERCE_S = 0x85
    ASTYPE = 0x86
    ASTYPELATE = 0x87
    NEGATE = 0x90
    INCREMENT = 0x91
    INCLOCAL = 0x92
    DECREMENT = 0x93
    DECLOCAL = 0x94
    TYPEOF = 0x95
    NOT = 0x96
    BITNOT = 0x97
    ADD = 0xA0
    SUBTRACT = 0xA1
    MULTIPLY = 0xA2
    DIVIDE = 0xA3
    MODULO = 0xA4
    LSHIFT = 0xA5
    RSHIFT = 0xA6
    URSHIFT = 0xA7
    BITAND = 0xA8
    BITOR = 0xA9
    BITXOR = 0xAA
    EQUALS = 0xAB
    STRICTEQUALS = 0xAC
    LESSTHAN = 0xAD
    LESSEQUALS = 0xAE
    GREATERTHAN = 0xAF
    GREATEREQUALS = 0xB0
    INSTANCEOF = 0xB1
    ISTYPE = 0xB2
    ISTYPELATE = 0xB3
    IN = 0xB4
    INCREMENT_I = 0xC0
    DECREMENT_I = 0xC1
    INCLOCAL_I = 0xC2
    DECLOCAL_I = 0xC3
    NEGATE_I = 0xC4
    ADD_I = 0xC5
    SUBTRACT_I = 0xC6
    MULTIPLY_I = 0xC7
    GETLOCAL0 = 0xD0
    GETLOCAL1 = 0xD1
    GETLOCAL2 = 0xD2
    GETLOCAL3 = 0xD3
    SETLOCAL0 = 0xD4
    SETLOCAL1 = 0xD5
    SETLOCAL2 = 0xD6
    SETLOCAL3 = 0xD7
    DEBUG = 0xEF
    DEBUGLINE = 0xF0
    DEBUGFILE = 0xF1
    BKPTLINE = 0xF2
    TIMESTAMP = 0xF3


# Operand formats:
#   ""   none
#   "u"  u30 (an index or a count)          "m"  u30 multiname index
#   "uu" / "mu"  two u30s
#   "b"  signed byte                        "B"  unsigned byte
#   "s"  s24 branch offset                  "S"  lookupswitch: s24 default, u30 case count, s24 offsets
#   "dbg" the debug instruction's operands
_WITH_OPERANDS = {
    "m": (Op.GETSUPER, Op.SETSUPER, Op.GETDESCENDANTS, Op.FINDPROPSTRICT, Op.FINDPROPERTY, Op.FINDDEF, Op.GETLEX,
        Op.SETPROPERTY, Op.GETPROPERTY, Op.INITPROPERTY, Op.DELETEPROPERTY, Op.COERCE, Op.ASTYPE, Op.ISTYPE),
    "u": (Op.DXNS, Op.KILL, Op.PUSHSHORT, Op.PUSHSTRING, Op.PUSHINT, Op.PUSHUINT, Op.PUSHDOUBLE, Op.PUSHNAMESPACE,
        Op.NEWFUNCTION, Op.CALL, Op.CONSTRUCT, Op.CONSTRUCTSUPER, Op.APPLYTYPE, Op.NEWOBJECT, Op.NEWARRAY,
        Op.NEWCLASS, Op.NEWCATCH, Op.GETLOCAL, Op.SETLOCAL, Op.GETSLOT, Op.SETSLOT, Op.GETGLOBALSLOT,
        Op.SETGLOBALSLOT, Op.INCLOCAL, Op.DECLOCAL, Op.INCLOCAL_I, Op.DECLOCAL_I, Op.DEBUGLINE, Op.DEBUGFILE,
        Op.BKPTLINE),
    "s": (Op.IFNLT, Op.IFNLE, Op.IFNGT, Op.IFNGE, Op.JUMP, Op.IFTRUE, Op.IFFALSE, Op.IFEQ, Op.IFNE, Op.IFLT, Op.IFLE,
        Op.IFGT, Op.IFGE, Op.IFSTRICTEQ, Op.IFSTRICTNE),
    "S": (Op.LOOKUPSWITCH,),
    "b": (Op.PUSHBYTE,),
    "uu": (Op.HASNEXT2, Op.CALLMETHOD, Op.CALLSTATIC),
    "mu": (Op.CALLSUPER, Op.CALLPROPERTY, Op.CONSTRUCTPROP, Op.CALLPROPLEX, Op.CALLSUPERVOID, Op.CALLPROPVOID),
    "B": (Op.GETSCOPEOBJECT,),
    "dbg": (Op.DEBUG,),
}
OPERANDS = {op: "" for op in Op} | {op: operands for operands, ops in _WITH_OPERANDS.items() for op in ops}
# By opcode byte: (the member, its operand format), so disassemble needs no Op(byte)
_DECODE = {int(op): (op, operands) for op, operands in OPERANDS.items()}

BRANCH_OPCODES = frozenset(op for op, operands in OPERANDS.items() if operands == "s")

# Instructions without any effect on the program's behaviour
NO_OPS = frozenset({Op.DEBUGLINE, Op.DEBUGFILE, Op.DEBUG, Op.NOP})
# ... plus label, which marks a backwards branch target
SKIPPABLE_OPS = NO_OPS | {Op.LABEL}

INTEGER_PUSHES = frozenset({Op.PUSHBYTE, Op.PUSHSHORT})
PROPERTY_WRITES = frozenset({Op.SETPROPERTY, Op.INITPROPERTY})
GETLOCALS = frozenset({Op.GETLOCAL, Op.GETLOCAL0, Op.GETLOCAL1, Op.GETLOCAL2, Op.GETLOCAL3})
SETLOCALS = frozenset({Op.SETLOCAL, Op.SETLOCAL0, Op.SETLOCAL1, Op.SETLOCAL2, Op.SETLOCAL3})
# The register of each getlocalN/setlocalN
_FIXED_REGISTER = {op: n for ops in ((Op.GETLOCAL0, Op.GETLOCAL1, Op.GETLOCAL2, Op.GETLOCAL3),
                                     (Op.SETLOCAL0, Op.SETLOCAL1, Op.SETLOCAL2, Op.SETLOCAL3))
                   for n, op in enumerate(ops)}
_NAMES = {op: op.name.lower() for op in Op}


@dataclass(slots=True)
class Instruction:
    offset: int     # within the method's code
    opcode: Op
    args: list[int]
    end: int        # offset of the next instruction

    @property
    def name(self) -> str:
        """The instruction's name, as in Op"""
        return _NAMES[self.opcode]

    @property
    def is_branch(self) -> bool:
        return self.opcode in BRANCH_OPCODES

    @property
    def is_lookupswitch(self) -> bool:
        return self.opcode is Op.LOOKUPSWITCH

    def jump_targets(self) -> list[int]:
        """Code offsets this instruction can jump to (empty unless it is a branch or lookupswitch)"""
        if self.is_branch:
            return [self.end + self.args[0]]
        if self.is_lookupswitch:
            # Unlike branches, lookupswitch offsets are relative to the instruction's own start
            default, _case_count, *cases = self.args
            return [self.offset + default] + [self.offset + case for case in cases]
        return []


@dataclass(frozen=True, slots=True)
class PushDouble:
    """An edit that replaces a push instruction with pushdouble of this constant pool entry"""
    pool_index: int


def disassemble(code: bytes) -> list[Instruction]:
    # Reads the code directly rather than through an AbcReader: the patcher disassembles every method
    # of every file (about 5,000 per file), so this is one of its hot loops
    instructions = []
    offset = 0
    while offset < len(code):
        start = offset
        opcode = code[offset]
        offset += 1
        if opcode not in _DECODE:
            raise ValueError(f"unknown opcode 0x{opcode:02x} at {start}")
        op, operands = _DECODE[opcode]
        if not operands:
            args = []
        elif operands in ("m", "u"):
            value, offset = read_u30(code, offset)
            args = [value]
        elif operands in ("uu", "mu"):
            first, offset = read_u30(code, offset)
            second, offset = read_u30(code, offset)
            args = [first, second]
        elif operands == "b":
            value = code[offset]
            offset += 1
            args = [value - 256 if value > 127 else value]
        elif operands == "B":
            args = [code[offset]]
            offset += 1
        elif operands == "s":
            args = [read_s24(code, offset)]
            offset += 3
        elif operands == "S":
            default = read_s24(code, offset)
            case_count, offset = read_u30(code, offset + 3)
            args = [default, case_count] + [read_s24(code, offset + 3 * i) for i in range(case_count + 1)]
            offset += 3 * (case_count + 1)
        else:   # "dbg"
            debug_type = code[offset]
            index, offset = read_u30(code, offset + 1)
            register = code[offset]
            extra, offset = read_u30(code, offset + 1)
            args = [debug_type, index, register, extra]
        instructions.append(Instruction(start, op, args, offset))
    return instructions


def assemble(code: bytes, instructions: list[Instruction], edits: dict, exceptions: list) -> tuple[bytes, list]:
    """Re-assemble a method with some instructions replaced.

    edits maps instruction index -> new value: an int (becomes pushbyte/pushshort), PushDouble, or the
    raw bytes of whatever replaces it (b"" to drop the instruction; nothing may branch into the middle
    of the instructions that are replaced together).
    Untouched instructions keep their original bytes, except that branch and lookupswitch offsets
    are recomputed, since the edits can change instruction sizes. Exception ranges are remapped
    too. Returns (new code, new exception table)."""
    pieces = []
    for i, ins in enumerate(instructions):
        if i in edits:
            pieces.append(_encode_push(edits[i]))
        elif ins.is_branch:
            pieces.append(b"\0" * 4)    # placeholder of the right size; filled in below
        elif ins.is_lookupswitch:
            case_count = ins.args[1]
            pieces.append(b"\0" * 4 + encode_u30(case_count) + b"\0" * 3 * (case_count + 1))
        else:
            pieces.append(code[ins.offset:ins.end])

    new_offset = {}     # old code offset -> new code offset
    position = 0
    for ins, piece in zip(instructions, pieces):
        new_offset[ins.offset] = position
        position += len(piece)
    new_offset[len(code)] = position

    for i, ins in enumerate(instructions):
        if i in edits:
            continue
        if ins.is_branch:
            target = new_offset[ins.jump_targets()[0]]
            branch_end = new_offset[ins.offset] + 4
            pieces[i] = bytes([ins.opcode]) + encode_s24(target - branch_end)
        elif ins.is_lookupswitch:
            base = new_offset[ins.offset]
            default, *cases = [new_offset[target] - base for target in ins.jump_targets()]
            pieces[i] = (bytes([Op.LOOKUPSWITCH]) + encode_s24(default) + encode_u30(ins.args[1])
                         + b"".join(encode_s24(case) for case in cases))

    new_exceptions = [[new_offset[start], new_offset[end], new_offset[target], exc_type, var_name]
                      for start, end, target, exc_type, var_name in exceptions]
    return b"".join(pieces), new_exceptions


def _encode_push(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, PushDouble):
        return bytes([Op.PUSHDOUBLE]) + encode_u30(value.pool_index)
    if -128 <= value <= 127:
        return bytes([Op.PUSHBYTE, value & 0xFF])
    return bytes([Op.PUSHSHORT]) + encode_u30(value)


def branch_targets(instructions: list[Instruction]) -> set[int]:
    """Every code offset that some branch or lookupswitch in the method can jump to"""
    return {target for ins in instructions for target in ins.jump_targets()}


def integer_constant(abc: AbcFile, ins: Instruction) -> int | None:
    """The value of a pushbyte/pushshort/pushint instruction, otherwise None"""
    if ins.opcode in INTEGER_PUSHES:
        return ins.args[0]
    if ins.opcode is Op.PUSHINT:
        return abc.ints[ins.args[0]]
    return None


def local_register(ins: Instruction) -> int:
    """The register number of a getlocal/getlocalN/setlocal/setlocalN instruction"""
    return ins.args[0] if ins.opcode is Op.GETLOCAL or ins.opcode is Op.SETLOCAL else _FIXED_REGISTER[ins.opcode]


def read_local(ins: Instruction) -> int | None:
    """The register a getlocal/getlocalN instruction reads, otherwise None"""
    return local_register(ins) if ins.opcode in GETLOCALS else None


def encode_getlocal(local: int) -> bytes:
    """A getlocal instruction for this register (getlocalN where there is one)"""
    return bytes([Op.GETLOCAL0 + local]) if local <= 3 else bytes([Op.GETLOCAL]) + encode_u30(local)


# --- Minimal stack model, used to find the operand expressions of a call ---

PUSHES_ONE = frozenset({
    Op.PUSHNULL, Op.PUSHUNDEFINED, Op.PUSHTRUE, Op.PUSHFALSE, Op.PUSHNAN, Op.PUSHBYTE, Op.PUSHSHORT,
    Op.PUSHSTRING, Op.PUSHINT, Op.PUSHUINT, Op.PUSHDOUBLE, Op.PUSHNAMESPACE, Op.GETLOCAL, Op.GETLOCAL0,
    Op.GETLOCAL1, Op.GETLOCAL2, Op.GETLOCAL3, Op.GETGLOBALSCOPE, Op.GETSCOPEOBJECT, Op.GETGLOBALSLOT,
    Op.NEWFUNCTION, Op.NEWACTIVATION, Op.NEWCATCH, Op.HASNEXT2,
})
UNARY = frozenset({
    Op.CONVERT_S, Op.CONVERT_I, Op.CONVERT_U, Op.CONVERT_D, Op.CONVERT_B, Op.CONVERT_O, Op.COERCE, Op.COERCE_A,
    Op.COERCE_S, Op.ASTYPE, Op.ISTYPE, Op.TYPEOF, Op.NOT, Op.NEGATE, Op.INCREMENT, Op.DECREMENT, Op.BITNOT,
    Op.GETSLOT, Op.CHECKFILTER, Op.ESC_XELEM, Op.ESC_XATTR, Op.NEGATE_I, Op.INCREMENT_I, Op.DECREMENT_I,
    Op.NEWCLASS,
})
BINARY = frozenset({
    Op.ADD, Op.SUBTRACT, Op.MULTIPLY, Op.DIVIDE, Op.MODULO, Op.LSHIFT, Op.RSHIFT, Op.URSHIFT, Op.BITAND, Op.BITOR,
    Op.BITXOR, Op.EQUALS, Op.STRICTEQUALS, Op.LESSTHAN, Op.LESSEQUALS, Op.GREATERTHAN, Op.GREATEREQUALS, Op.ADD_I,
    Op.SUBTRACT_I, Op.MULTIPLY_I, Op.INSTANCEOF, Op.IN, Op.ASTYPELATE, Op.ISTYPELATE, Op.NEXTNAME, Op.NEXTVALUE,
    Op.HASNEXT,
})
def stack_effect(abc: AbcFile, ins: Instruction) -> tuple[int, int] | None:
    """(values popped, values pushed), or None for control flow and unmodelled instructions"""
    op, args = ins.opcode, ins.args
    if op in PUSHES_ONE:
        return 0, 1
    if op in UNARY:
        return 1, 1
    if op in BINARY:
        return 2, 1
    if op is Op.DUP:
        return 1, 2
    if op is Op.SWAP:
        return 2, 2
    if op in (Op.GETPROPERTY, Op.GETSUPER, Op.GETDESCENDANTS):
        return 1 + abc.runtime_name_operands(args[0]), 1
    if op in (Op.GETLEX, Op.FINDPROPSTRICT, Op.FINDPROPERTY, Op.FINDDEF):
        return abc.runtime_name_operands(args[0]), 1
    if op in (Op.CALLPROPERTY, Op.CALLPROPLEX, Op.CALLSUPER, Op.CONSTRUCTPROP):
        return args[1] + 1 + abc.runtime_name_operands(args[0]), 1
    if op in (Op.CALLPROPVOID, Op.CALLSUPERVOID):
        return args[1] + 1 + abc.runtime_name_operands(args[0]), 0
    if op is Op.CONSTRUCT:
        return args[0] + 1, 1
    if op is Op.CALL:
        return args[0] + 2, 1
    if op is Op.APPLYTYPE:
        return args[0] + 1, 1
    if op is Op.NEWARRAY:
        return args[0], 1
    if op is Op.NEWOBJECT:
        return args[0] * 2, 1
    if op in (Op.CALLMETHOD, Op.CALLSTATIC):
        return args[1] + 1, 1
    if op in (Op.INCLOCAL, Op.DECLOCAL, Op.INCLOCAL_I, Op.DECLOCAL_I, Op.KILL):
        return 0, 0
    return None


def expression_start(abc: AbcFile, instructions: list[Instruction], end: int) -> int | None:
    """Index of the first instruction of the expression that produces the single stack value
    consumed right before instructions[end].

    None if that expression involves control flow or an instruction the stack model doesn't know."""
    needed = 1
    for j in range(end - 1, -1, -1):
        ins = instructions[j]
        if ins.opcode in NO_OPS:
            continue
        if ins.opcode is Op.LABEL or ins.is_branch or ins.is_lookupswitch:
            return None
        effect = stack_effect(abc, ins)
        if effect is None:
            return None
        popped, pushed = effect
        needed += popped - pushed
        if needed <= 0:
            return j if needed == 0 else None
    return None


def skip_no_ops_backwards(instructions: list[Instruction], index: int) -> int:
    """The nearest index <= `index` that is not a debug/nop instruction (-1 if there is none)"""
    while index >= 0 and instructions[index].opcode in NO_OPS:
        index -= 1
    return index
