"""AVM2 method bytecode: disassembly, re-assembly with replaced constants, and a small stack model"""

from __future__ import annotations

from dataclasses import dataclass

from .abc_file import AbcFile, AbcReader, encode_s24, encode_u30

# Operand formats:
#   ""   none
#   "u"  u30 (an index or a count)          "m"  u30 multiname index
#   "uu" / "mu"  two u30s
#   "b"  signed byte                        "B"  unsigned byte
#   "s"  s24 branch offset                  "S"  lookupswitch: s24 default, u30 case count, s24 offsets
#   "dbg" the debug instruction's operands
OPCODES = {
    0x01: ("bkpt", ""), 0x02: ("nop", ""), 0x03: ("throw", ""), 0x04: ("getsuper", "m"),
    0x05: ("setsuper", "m"), 0x06: ("dxns", "u"), 0x07: ("dxnslate", ""), 0x08: ("kill", "u"),
    0x09: ("label", ""), 0x0C: ("ifnlt", "s"), 0x0D: ("ifnle", "s"), 0x0E: ("ifngt", "s"),
    0x0F: ("ifnge", "s"), 0x10: ("jump", "s"), 0x11: ("iftrue", "s"), 0x12: ("iffalse", "s"),
    0x13: ("ifeq", "s"), 0x14: ("ifne", "s"), 0x15: ("iflt", "s"), 0x16: ("ifle", "s"),
    0x17: ("ifgt", "s"), 0x18: ("ifge", "s"), 0x19: ("ifstricteq", "s"), 0x1A: ("ifstrictne", "s"),
    0x1B: ("lookupswitch", "S"), 0x1C: ("pushwith", ""), 0x1D: ("popscope", ""), 0x1E: ("nextname", ""),
    0x1F: ("hasnext", ""), 0x20: ("pushnull", ""), 0x21: ("pushundefined", ""), 0x23: ("nextvalue", ""),
    0x24: ("pushbyte", "b"), 0x25: ("pushshort", "u"), 0x26: ("pushtrue", ""), 0x27: ("pushfalse", ""),
    0x28: ("pushnan", ""), 0x29: ("pop", ""), 0x2A: ("dup", ""), 0x2B: ("swap", ""),
    0x2C: ("pushstring", "u"), 0x2D: ("pushint", "u"), 0x2E: ("pushuint", "u"), 0x2F: ("pushdouble", "u"),
    0x30: ("pushscope", ""), 0x31: ("pushnamespace", "u"), 0x32: ("hasnext2", "uu"),
    0x40: ("newfunction", "u"), 0x41: ("call", "u"), 0x42: ("construct", "u"), 0x43: ("callmethod", "uu"),
    0x44: ("callstatic", "uu"), 0x45: ("callsuper", "mu"), 0x46: ("callproperty", "mu"),
    0x47: ("returnvoid", ""), 0x48: ("returnvalue", ""), 0x49: ("constructsuper", "u"),
    0x4A: ("constructprop", "mu"), 0x4C: ("callproplex", "mu"), 0x4E: ("callsupervoid", "mu"),
    0x4F: ("callpropvoid", "mu"), 0x53: ("applytype", "u"), 0x55: ("newobject", "u"),
    0x56: ("newarray", "u"), 0x57: ("newactivation", ""), 0x58: ("newclass", "u"),
    0x59: ("getdescendants", "m"), 0x5A: ("newcatch", "u"), 0x5D: ("findpropstrict", "m"),
    0x5E: ("findproperty", "m"), 0x5F: ("finddef", "m"), 0x60: ("getlex", "m"), 0x61: ("setproperty", "m"),
    0x62: ("getlocal", "u"), 0x63: ("setlocal", "u"), 0x64: ("getglobalscope", ""),
    0x65: ("getscopeobject", "B"), 0x66: ("getproperty", "m"), 0x68: ("initproperty", "m"),
    0x6A: ("deleteproperty", "m"), 0x6C: ("getslot", "u"), 0x6D: ("setslot", "u"),
    0x6E: ("getglobalslot", "u"), 0x6F: ("setglobalslot", "u"), 0x70: ("convert_s", ""),
    0x71: ("esc_xelem", ""), 0x72: ("esc_xattr", ""), 0x73: ("convert_i", ""), 0x74: ("convert_u", ""),
    0x75: ("convert_d", ""), 0x76: ("convert_b", ""), 0x77: ("convert_o", ""), 0x78: ("checkfilter", ""),
    0x80: ("coerce", "m"), 0x82: ("coerce_a", ""), 0x85: ("coerce_s", ""), 0x86: ("astype", "m"),
    0x87: ("astypelate", ""), 0x90: ("negate", ""), 0x91: ("increment", ""), 0x92: ("inclocal", "u"),
    0x93: ("decrement", ""), 0x94: ("declocal", "u"), 0x95: ("typeof", ""), 0x96: ("not", ""),
    0x97: ("bitnot", ""), 0xA0: ("add", ""), 0xA1: ("subtract", ""), 0xA2: ("multiply", ""),
    0xA3: ("divide", ""), 0xA4: ("modulo", ""), 0xA5: ("lshift", ""), 0xA6: ("rshift", ""),
    0xA7: ("urshift", ""), 0xA8: ("bitand", ""), 0xA9: ("bitor", ""), 0xAA: ("bitxor", ""),
    0xAB: ("equals", ""), 0xAC: ("strictequals", ""), 0xAD: ("lessthan", ""), 0xAE: ("lessequals", ""),
    0xAF: ("greaterthan", ""), 0xB0: ("greaterequals", ""), 0xB1: ("instanceof", ""), 0xB2: ("istype", "m"),
    0xB3: ("istypelate", ""), 0xB4: ("in", ""), 0xC0: ("increment_i", ""), 0xC1: ("decrement_i", ""),
    0xC2: ("inclocal_i", "u"), 0xC3: ("declocal_i", "u"), 0xC4: ("negate_i", ""), 0xC5: ("add_i", ""),
    0xC6: ("subtract_i", ""), 0xC7: ("multiply_i", ""), 0xD0: ("getlocal0", ""), 0xD1: ("getlocal1", ""),
    0xD2: ("getlocal2", ""), 0xD3: ("getlocal3", ""), 0xD4: ("setlocal0", ""), 0xD5: ("setlocal1", ""),
    0xD6: ("setlocal2", ""), 0xD7: ("setlocal3", ""), 0xEF: ("debug", "dbg"), 0xF0: ("debugline", "u"),
    0xF1: ("debugfile", "u"), 0xF2: ("bkptline", "u"), 0xF3: ("timestamp", ""),
}
# Alchemy memory opcodes (no operands)
for _opcode in range(0x35, 0x40):
    OPCODES.setdefault(_opcode, (f"alchemy{_opcode:02x}", ""))

OP_PUSHBYTE = 0x24
OP_PUSHSHORT = 0x25
OP_PUSHDOUBLE = 0x2F
OP_LOOKUPSWITCH = 0x1B

BRANCH_OPCODES = frozenset(op for op, (_, operands) in OPCODES.items() if operands == "s")

# Instructions without any effect on the program's behaviour
NO_OP_NAMES = frozenset({"debugline", "debugfile", "debug", "nop"})
# ... plus "label", which marks a backwards branch target
SKIPPABLE_NAMES = NO_OP_NAMES | {"label"}

INTEGER_PUSH_NAMES = ("pushbyte", "pushshort")


@dataclass
class Instruction:
    offset: int     # within the method's code
    opcode: int
    name: str
    args: list[int]
    end: int        # offset of the next instruction

    @property
    def is_branch(self) -> bool:
        return self.opcode in BRANCH_OPCODES

    @property
    def is_lookupswitch(self) -> bool:
        return self.opcode == OP_LOOKUPSWITCH

    def jump_targets(self) -> list[int]:
        """Code offsets this instruction can jump to (empty unless it is a branch or lookupswitch)"""
        if self.is_branch:
            return [self.end + self.args[0]]
        if self.is_lookupswitch:
            # Unlike branches, lookupswitch offsets are relative to the instruction's own start
            default, _case_count, *cases = self.args
            return [self.offset + default] + [self.offset + case for case in cases]
        return []


@dataclass(frozen=True)
class PushDouble:
    """An edit that replaces a push instruction with pushdouble of this constant pool entry"""
    pool_index: int


def disassemble(code: bytes) -> list[Instruction]:
    r = AbcReader(code)
    instructions = []
    while r.offset < len(code):
        offset = r.offset
        opcode = r.u8()
        if opcode not in OPCODES:
            raise ValueError(f"unknown opcode 0x{opcode:02x} at {offset}")
        name, operands = OPCODES[opcode]
        if operands in ("m", "u"):
            args = [r.u30()]
        elif operands in ("uu", "mu"):
            args = [r.u30(), r.u30()]
        elif operands == "b":
            value = r.u8()
            args = [value - 256 if value > 127 else value]
        elif operands == "B":
            args = [r.u8()]
        elif operands == "s":
            args = [r.s24()]
        elif operands == "S":
            default = r.s24()
            case_count = r.u30()
            args = [default, case_count] + [r.s24() for _ in range(case_count + 1)]
        elif operands == "dbg":
            args = [r.u8(), r.u30(), r.u8(), r.u30()]
        else:
            args = []
        instructions.append(Instruction(offset, opcode, name, args, r.offset))
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
            pieces[i] = (bytes([OP_LOOKUPSWITCH]) + encode_s24(default) + encode_u30(ins.args[1])
                         + b"".join(encode_s24(case) for case in cases))

    new_exceptions = [[new_offset[start], new_offset[end], new_offset[target], exc_type, var_name]
                      for start, end, target, exc_type, var_name in exceptions]
    return b"".join(pieces), new_exceptions


def _encode_push(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, PushDouble):
        return bytes([OP_PUSHDOUBLE]) + encode_u30(value.pool_index)
    if -128 <= value <= 127:
        return bytes([OP_PUSHBYTE, value & 0xFF])
    return bytes([OP_PUSHSHORT]) + encode_u30(value)


def branch_targets(instructions: list[Instruction]) -> set[int]:
    """Every code offset that some branch or lookupswitch in the method can jump to"""
    return {target for ins in instructions for target in ins.jump_targets()}


def integer_constant(abc: AbcFile, ins: Instruction) -> int | None:
    """The value of a pushbyte/pushshort/pushint instruction, otherwise None"""
    if ins.name in INTEGER_PUSH_NAMES:
        return ins.args[0]
    if ins.name == "pushint":
        return abc.ints[ins.args[0]]
    return None


def local_register(ins: Instruction) -> int:
    """The register number of a getlocal/getlocalN/setlocal/setlocalN instruction"""
    return ins.args[0] if ins.name in ("getlocal", "setlocal") else int(ins.name[-1])


# --- Minimal stack model, used to find the operand expressions of a call ---

PUSHES_ONE = frozenset({
    "pushnull", "pushundefined", "pushtrue", "pushfalse", "pushnan", "pushbyte", "pushshort",
    "pushstring", "pushint", "pushuint", "pushdouble", "pushnamespace", "getlocal", "getlocal0",
    "getlocal1", "getlocal2", "getlocal3", "getglobalscope", "getscopeobject", "getglobalslot",
    "newfunction", "newactivation", "newcatch", "hasnext2",
})
UNARY = frozenset({
    "convert_s", "convert_i", "convert_u", "convert_d", "convert_b", "convert_o", "coerce", "coerce_a",
    "coerce_s", "astype", "istype", "typeof", "not", "negate", "increment", "decrement", "bitnot",
    "getslot", "checkfilter", "esc_xelem", "esc_xattr", "negate_i", "increment_i", "decrement_i",
    "newclass",
})
BINARY = frozenset({
    "add", "subtract", "multiply", "divide", "modulo", "lshift", "rshift", "urshift", "bitand", "bitor",
    "bitxor", "equals", "strictequals", "lessthan", "lessequals", "greaterthan", "greaterequals", "add_i",
    "subtract_i", "multiply_i", "instanceof", "in", "astypelate", "istypelate", "nextname", "nextvalue",
    "hasnext",
})


def stack_effect(abc: AbcFile, ins: Instruction) -> tuple[int, int] | None:
    """(values popped, values pushed), or None for control flow and unmodelled instructions"""
    name, args = ins.name, ins.args
    if name in PUSHES_ONE:
        return 0, 1
    if name in UNARY:
        return 1, 1
    if name in BINARY:
        return 2, 1
    if name == "dup":
        return 1, 2
    if name == "swap":
        return 2, 2
    if name in ("getproperty", "getsuper", "getdescendants"):
        return 1 + abc.runtime_name_operands(args[0]), 1
    if name in ("getlex", "findpropstrict", "findproperty", "finddef"):
        return abc.runtime_name_operands(args[0]), 1
    if name in ("callproperty", "callproplex", "callsuper", "constructprop"):
        return args[1] + 1 + abc.runtime_name_operands(args[0]), 1
    if name in ("callpropvoid", "callsupervoid"):
        return args[1] + 1 + abc.runtime_name_operands(args[0]), 0
    if name == "construct":
        return args[0] + 1, 1
    if name == "call":
        return args[0] + 2, 1
    if name == "applytype":
        return args[0] + 1, 1
    if name == "newarray":
        return args[0], 1
    if name == "newobject":
        return args[0] * 2, 1
    if name in ("callmethod", "callstatic"):
        return args[1] + 1, 1
    if name in ("inclocal", "declocal", "inclocal_i", "declocal_i", "kill"):
        return 0, 0
    return None


def expression_start(abc: AbcFile, instructions: list[Instruction], end: int) -> int | None:
    """Index of the first instruction of the expression that produces the single stack value
    consumed right before instructions[end].

    None if that expression involves control flow or an instruction the stack model doesn't know."""
    needed = 1
    for j in range(end - 1, -1, -1):
        ins = instructions[j]
        if ins.name in NO_OP_NAMES:
            continue
        if ins.name == "label" or ins.is_branch or ins.is_lookupswitch:
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
    while index >= 0 and instructions[index].name in NO_OP_NAMES:
        index -= 1
    return index
