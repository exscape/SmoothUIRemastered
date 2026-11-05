"""Glossary and Journal > Quests: the right stick scrolls text far too fast at high frame rates.

TextAreaModuleCustomInput (the text of Glossary entries and of Journal > Quests) scrolls with the right
stick by moving its scroll bar by the stick's tilt, in lines, on every stick event. Those come every
frame while the stick is tilted, so the speed follows the frame rate: at 120 fps, a slight touch of the
stick scrolls to the end. (The scroll bar also rounds to whole lines, so any tilt above half scrolled a line
per frame, and less did nothing.)

The fix adds the class smoothui.text.StickScroll (text_stick_scroll_as3/, compiled into
text_stick_scroll.abc with dev-tools/build-as3.py) as a DoABC tag of its own, and makes the handler call
StickScroll.scroll(this, tilt), which scrolls at a speed in pixels per second (see StickScroll.as). The
mouse wheel and the scroll bar work as before.
"""

from __future__ import annotations

import struct
from pathlib import Path

from ..swf_patcher.abc_bytecode import (
    NO_OP_NAMES,
    Instruction,
    assemble,
    branch_targets,
    disassemble,
)
from ..swf_patcher.abc_file import (
    PACKAGE_NAMESPACE,
    AbcFile,
    PoolAdditions,
    encode_u30,
    parse_abc,
)
from ..swf_patcher.swf_tags import Tag, TagCode
from .swf_edit import AFTER_RESCALE, CantApply, FixKind, FixResult, SwfEdit

NAME = "text-stick-scroll-fix"
CONSEQUENCE = "in Glossary and Journal > Quests, the right stick scrolls text too fast (speed depends on FPS)"
KIND = FixKind.GAME_BUG
# Code only: the rescale needn't see it
PHASE = AFTER_RESCALE

ABC_PATH = Path(__file__).with_name("text_stick_scroll.abc")
PACKAGE, CLASS, METHOD = "smoothui.text", "StickScroll", "scroll"
TEXT_CLASS = "red.game.witcher3.menus.common.TextAreaModuleCustomInput"
HANDLER = "handleCustomInput"
# DoABC flags: kDoAbcLazyInitializeFlag, as Flash sets it
DO_ABC_LAZY = 1

OP_GETLEX, OP_GETLOCAL0, OP_GETLOCAL, OP_CALLPROPVOID = 0x60, 0xD0, 0x62, 0x4F


def apply(swf: SwfEdit) -> FixResult:
    unit = swf.unit_of_class(TEXT_CLASS)
    abc = unit.abc
    instance = next(i for i in abc.instances if abc.full_name_of(i.name) == TEXT_CLASS)
    method = next((t.method for t in instance.traits if t.method is not None and abc.name_of(t.name) == HANDLER),
                  None)
    if method is None or method not in abc.bodies:
        raise CantApply(f"{TEXT_CLASS} has no method {HANDLER}")
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    found = _find_scroll(abc, instructions)
    if found is None:
        if any(ins.name == "callpropvoid" and abc.name_of(ins.args[0]) == METHOD for ins in instructions):
            return FixResult([], f"{TEXT_CLASS}.{HANDLER} already calls {CLASS}.{METHOD}")
        raise CantApply(f"{TEXT_CLASS}.{HANDLER} doesn't do mcScrollbar.position -= <tilt>")
    first, last, tilt = found
    inner = {ins.offset for ins in instructions[first + 1:last + 1]}
    if inner & branch_targets(instructions):
        raise CantApply(f"{HANDLER} has a branch into mcScrollbar.position -= <tilt>")

    ours = _abc_bytes()
    if clash := swf.class_names() & {parse_abc(ours).full_name_of(i.name) for i in parse_abc(ours).instances}:
        raise CantApply(f"the file already defines {', '.join(sorted(clash))}")
    sprites = swf.sprites_of_class_or_subclasses(abc, instance.name)
    if not sprites:
        raise CantApply(f"no sprite is linked to {TEXT_CLASS} or a class that extends it")

    additions = PoolAdditions(abc)
    scroll_class = additions.qname(additions.namespace(PACKAGE_NAMESPACE, PACKAGE), CLASS)
    scroll = additions.qname(additions.namespace(PACKAGE_NAMESPACE, ""), METHOD)
    # StickScroll.scroll(this, tilt): at most 3 values on the stack, as the code it replaces
    call = (bytes([OP_GETLEX]) + encode_u30(scroll_class) + bytes([OP_GETLOCAL0]) + _getlocal(tilt)
            + bytes([OP_CALLPROPVOID]) + encode_u30(scroll) + encode_u30(2))
    edits = {first: call, **{i: b"" for i in range(first + 1, last + 1)}}
    unit.replace_methods({method: assemble(body.code, instructions, edits, body.exceptions)}, additions)
    swf.insert_abc(Tag(TagCode.DO_ABC, struct.pack("<I", DO_ABC_LAZY) + PACKAGE.encode() + b"\0" + ours,
                       long_header=True))
    return FixResult(sprites, f"added {PACKAGE}.{CLASS}; {HANDLER} scrolls with it, at a speed in pixels per "
                              "second rather than a tilt in lines per frame")


def _abc_bytes() -> bytes:
    try:
        return ABC_PATH.read_bytes()
    except FileNotFoundError:
        raise CantApply(f"{ABC_PATH.name} is missing; build it with dev-tools/build-as3.py") from None


def _find_scroll(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int, int] | None:
    """(first, last instruction index, the tilt's local variable) of `mcScrollbar.position -= <local>`, the
    only such statement; None if there is none. Debug instructions in between are allowed."""
    real = [i for i, ins in enumerate(instructions) if ins.name not in NO_OP_NAMES]
    pattern = [("getlex", "mcScrollbar"), ("getlex", "mcScrollbar"), ("getproperty", "position"),
               ("getlocal", None), ("subtract", None), ("setproperty", "position")]
    matches = []
    for k in range(len(real) - len(pattern) + 1):
        window = [instructions[i] for i in real[k:k + len(pattern)]]
        if all(_matches(abc, ins, name, prop) for ins, (name, prop) in zip(window, pattern)):
            matches.append((real[k], real[k + len(pattern) - 1], _local_of(window[3])))
    return matches[0] if len(matches) == 1 else None


def _matches(abc: AbcFile, ins: Instruction, name: str, prop: str | None) -> bool:
    if name == "getlocal":
        return _local_of(ins) is not None
    return ins.name == name and (prop is None or abc.name_of(ins.args[0]) == prop)


def _local_of(ins: Instruction) -> int | None:
    """The local variable (register) a getlocal instruction reads"""
    if ins.name == "getlocal":
        return ins.args[0]
    if ins.name in ("getlocal0", "getlocal1", "getlocal2", "getlocal3"):
        return int(ins.name[-1])
    return None


def _getlocal(local: int) -> bytes:
    return bytes([OP_GETLOCAL0 + local]) if local <= 3 else bytes([OP_GETLOCAL]) + encode_u30(local)
