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
    NO_OP_NAMES,
    Instruction,
    assemble,
    branch_targets,
    disassemble,
)
from ..swf_patcher.abc_file import PACKAGE_NAMESPACE, AbcFile, PoolAdditions, encode_u30
from .swf_edit import AFTER_RESCALE, CantApply, FixKind, FixResult, SwfEdit
from .text_stick_scroll_fix import CLASS, PACKAGE, abc_bytes, insert_class

NAME = "book-stick-scroll-fix"
CONSEQUENCE = "in books, the left stick scrolls text slowly, a line at a time"
KIND = FixKind.GAME_BUG
# Code only: the rescale needn't see it
PHASE = AFTER_RESCALE

METHOD = "textAreaNavigation"
TEXT_CLASS = "scaleform.clik.controls.TextArea"
HANDLER = "handleInput"

OP_GETLEX, OP_GETLOCAL0, OP_GETLOCAL1, OP_CALLPROPERTY = 0x60, 0xD0, 0xD1, 0x46


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

    found = _find_navigation(abc, instructions)
    if found is None:
        if any(ins.name == "callproperty" and abc.name_of(ins.args[0]) == METHOD for ins in instructions):
            return FixResult([], f"{TEXT_CLASS}.{HANDLER} already calls {CLASS}.{METHOD}")
        raise CantApply(f"{TEXT_CLASS}.{HANDLER} doesn't read event.details.navEquivalent once")
    first, last = found
    inner = {ins.offset for ins in instructions[first + 1:last + 1]}
    if inner & branch_targets(instructions):
        raise CantApply(f"{HANDLER} has a branch into event.details.navEquivalent")

    ours = abc_bytes(swf)
    sprites = swf.sprites_of_class_or_subclasses(abc, instance.name)
    if not sprites:
        raise CantApply(f"no sprite is linked to {TEXT_CLASS} or a class that extends it")

    additions = PoolAdditions(abc)
    scroll_class = additions.qname(additions.namespace(PACKAGE_NAMESPACE, PACKAGE), CLASS)
    navigation = additions.qname(additions.namespace(PACKAGE_NAMESPACE, ""), METHOD)
    # StickScroll.textAreaNavigation(this, event) in place of event.details.navEquivalent: 3 values on the stack
    # at most, as in the method's `elapsed > AXIS_INPUT_DELAY * 2` after it
    call = (bytes([OP_GETLEX]) + encode_u30(scroll_class) + bytes([OP_GETLOCAL0, OP_GETLOCAL1, OP_CALLPROPERTY])
            + encode_u30(navigation) + encode_u30(2))
    edits = {first: call, **{i: b"" for i in range(first + 1, last + 1)}}
    unit.replace_methods({method: assemble(body.code, instructions, edits, body.exceptions)}, additions)
    insert_class(swf, ours)
    return FixResult(sprites, f"added {PACKAGE}.{CLASS}; {HANDLER} scrolls with the left stick with it, at a speed "
                              "in pixels per second rather than a line per key repeat")


def _find_navigation(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int] | None:
    """(first, last instruction index) of `event.details.navEquivalent` (event: the method's argument), the only
    one; None if there is none. Debug instructions in between are allowed."""
    real = [i for i, ins in enumerate(instructions) if ins.name not in NO_OP_NAMES]
    pattern = [("getlocal1", None), ("getproperty", "details"), ("getproperty", "navEquivalent")]
    matches = []
    for k in range(len(real) - len(pattern) + 1):
        window = [instructions[i] for i in real[k:k + len(pattern)]]
        if all(ins.name == name and (prop is None or abc.name_of(ins.args[0]) == prop)
               for ins, (name, prop) in zip(window, pattern)):
            matches.append((real[k], real[k + 2]))
    return matches[0] if len(matches) == 1 else None
