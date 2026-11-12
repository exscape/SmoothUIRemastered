"""Books: the left stick scrolls their text slowly, a line at a time.

CLIK's TextArea (the text of the book popup, opened from the inventory or when picking up a book) scrolls
with the left stick one line per up/down key event, which the stick sends at the engine's key repeat rate,
whatever the tilt. (Its right-stick code never scrolls with a gamepad: it compares the stick's direction
with the keyboard's up/down codes, but gets the d-pad's. The fix leaves it as it is.)

The fix adds the class smoothui.text.StickScroll (shared with text_stick_scroll_fix.py) and makes TextArea's
handleInput take the navigation code it acts on from StickScroll.textAreaNavigation(this, event), in place
of event.details.navEquivalent. That scrolls on the left stick's axis events, which come every frame, as
Glossary entries scroll with the right stick: at a speed in pixels per second that follows the tilt (see
StickScroll.as); and it gives the left stick's up/down key events no navigation code, so they no longer
step a line. The keyboard, the mouse wheel and the scroll bar work as before.
"""

from __future__ import annotations

from ..swf_patcher.abc_bytecode import (
    Instruction,
    Op,
    assemble,
    disassemble,
    encode,
    find_pattern,
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
from .text_stick_scroll_fix import ABC_PATH, CLASS, PACKAGE

METHOD = "textAreaNavigation"
TEXT_CLASS = "scaleform.clik.controls.TextArea"
HANDLER = "handleInput"



def apply(swf: SwfEdit) -> FixResult:
    unit = swf.unit_of_class(TEXT_CLASS)
    abc = unit.abc
    _, instance = unit.instance(TEXT_CLASS)
    method = unit.method(TEXT_CLASS, HANDLER)
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    found = _find_navigation(abc, instructions)
    if found is None:
        if any(ins.opcode is Op.CALLPROPERTY and abc.name_of(ins.args[0]) == METHOD for ins in instructions):
            return FixResult([], f"{TEXT_CLASS}.{HANDLER} already calls {CLASS}.{METHOD}")
        raise CantApply(f"{TEXT_CLASS}.{HANDLER} doesn't read event.details.navEquivalent once")
    additions = PoolAdditions(abc)
    scroll_class = additions.qname(additions.namespace(NamespaceKind.PACKAGE, PACKAGE), CLASS)
    navigation = additions.qname(additions.namespace(NamespaceKind.PACKAGE, ""), METHOD)
    # StickScroll.textAreaNavigation(this, event) in place of event.details.navEquivalent: 3 values on the stack
    # at most, as in the method's `elapsed > AXIS_INPUT_DELAY * 2` after it
    call = (encode(Op.GETLEX, scroll_class) + encode(Op.GETLOCAL0) + encode(Op.GETLOCAL1)
            + encode(Op.CALLPROPERTY, navigation, 2))
    edits = replace_run(instructions, *found, call, HANDLER, "event.details.navEquivalent")

    ours = read_abc(ABC_PATH)
    swf.check_new_classes(ours)
    sprites = swf.sprites_of_class_or_subclasses(abc, instance.name)
    if not sprites:
        raise CantApply(f"no sprite is linked to {TEXT_CLASS} or a class that extends it")

    unit.replace_methods({method: assemble(body.code, instructions, edits, body.exceptions)}, additions)
    swf.insert_abc(PACKAGE, ours)
    return FixResult(sprites, f"added {PACKAGE}.{CLASS}; {HANDLER} scrolls with the left stick with it, at a speed "
                              "in pixels per second rather than a line per key repeat")


def _find_navigation(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int] | None:
    """(first, last instruction index) of `event.details.navEquivalent` (event: the method's argument), the only
    one; None if there is none. Debug instructions in between are allowed."""
    found = find_pattern(abc, instructions, [(Op.GETLOCAL1, None), (Op.GETPROPERTY, "details"),
                                             (Op.GETPROPERTY, "navEquivalent")], skip_no_ops=True)
    return (found[0][0], found[0][-1]) if len(found) == 1 else None


FIX = Fix(name="book-stick-scroll-fix",
          kind=FixKind.GAME_BUG,
          consequence="in books, the left stick scrolls text slowly, a line at a time",
          apply=apply,
          # Code only: the rescale needn't see it
          phase=Phase.AFTER_RESCALE)
