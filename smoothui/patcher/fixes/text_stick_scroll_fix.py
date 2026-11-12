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

from pathlib import Path

from ..swf_patcher.abc_bytecode import (
    ANY_GETLOCAL,
    Instruction,
    Op,
    assemble,
    disassemble,
    encode,
    encode_getlocal,
    find_pattern,
    local_register,
)
from ..swf_patcher.abc_file import AbcFile, NamespaceKind, PoolAdditions
from .swf_edit import (
    CantApply,
    Fix,
    FixKind,
    FixResult,
    Phase,
    SwfEdit,
    read_abc,
    replace_run,
)

ABC_PATH = Path(__file__).with_name("text_stick_scroll.abc")
PACKAGE, CLASS, METHOD = "smoothui.text", "StickScroll", "scroll"
TEXT_CLASS = "red.game.witcher3.menus.common.TextAreaModuleCustomInput"
HANDLER = "handleCustomInput"


def apply(swf: SwfEdit) -> FixResult:
    unit = swf.unit_of_class(TEXT_CLASS)
    abc = unit.abc
    _, instance = unit.instance(TEXT_CLASS)
    method = unit.method(TEXT_CLASS, HANDLER)
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    found = _find_scroll(abc, instructions)
    if found is None:
        if any(ins.opcode is Op.CALLPROPVOID and abc.name_of(ins.args[0]) == METHOD for ins in instructions):
            return FixResult([], f"{TEXT_CLASS}.{HANDLER} already calls {CLASS}.{METHOD}")
        raise CantApply(f"{TEXT_CLASS}.{HANDLER} doesn't do mcScrollbar.position -= <tilt>")
    first, last, tilt = found
    additions = PoolAdditions(abc)
    scroll_class = additions.qname(additions.namespace(NamespaceKind.PACKAGE, PACKAGE), CLASS)
    scroll = additions.qname(additions.namespace(NamespaceKind.PACKAGE, ""), METHOD)
    # StickScroll.scroll(this, tilt): at most 3 values on the stack, as the code it replaces
    call = (encode(Op.GETLEX, scroll_class) + encode(Op.GETLOCAL0) + encode_getlocal(tilt)
            + encode(Op.CALLPROPVOID, scroll, 2))
    edits = replace_run(instructions, first, last, call, HANDLER, "mcScrollbar.position -= <tilt>")

    ours = read_abc(ABC_PATH)
    swf.check_new_classes(ours)
    sprites = swf.sprites_of_class_or_subclasses(abc, instance.name)
    if not sprites:
        raise CantApply(f"no sprite is linked to {TEXT_CLASS} or a class that extends it")

    unit.replace_methods({method: assemble(body.code, instructions, edits, body.exceptions)}, additions)
    swf.insert_abc(PACKAGE, ours)
    return FixResult(sprites, f"added {PACKAGE}.{CLASS}; {HANDLER} scrolls with it, at a speed in pixels per "
                              "second rather than a tilt in lines per frame")


def _find_scroll(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int, int] | None:
    """(first, last instruction index, the tilt's local variable) of `mcScrollbar.position -= <local>`, the
    only such statement; None if there is none. Debug instructions in between are allowed."""
    found = find_pattern(abc, instructions, [(Op.GETLEX, "mcScrollbar"), (Op.GETLEX, "mcScrollbar"),
                                             (Op.GETPROPERTY, "position"), ANY_GETLOCAL, (Op.SUBTRACT, None),
                                             (Op.SETPROPERTY, "position")], skip_no_ops=True)
    if len(found) != 1:
        return None
    run = found[0]
    return run[0], run[-1], local_register(instructions[run[3]])


FIX = Fix(name="text-stick-scroll-fix",
          kind=FixKind.GAME_BUG,
          consequence="in Glossary and Journal > Quests, the right stick scrolls text too fast (speed depends on FPS)",
          apply=apply,
          # Code only: the rescale needn't see it
          phase=Phase.AFTER_RESCALE)
