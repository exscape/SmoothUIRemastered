"""Menus: removes the slide when a menu opens and closes, so that it only fades. Optional: a matter of taste, so no
profile names it, and the merger applies it if the user chose to (see requires.OPTIONAL_FIXES).

Each file has its own copy of CoreMenu. Its showAnimation puts the menu SHOW_ANIM_OFFSET pixels lower (the static
initializer sets that: 500 in most files), then tweens y to 0 while it tweens alpha to 1. Its hideAnimation tweens y
to 200 while it tweens alpha to 0.

The fix makes both 0, as the constructors of some of the game's own menus do with the offset. (The slot's default
value says the same as the initializer, which overwrites it, so the fix leaves it.) The tweens still run, so the fades
and what happens when they end stay as they were. A copy without the slides has nothing to fix: a mod's copy mustn't
be reported as lacking a fix the user only chose as a look.
"""

from __future__ import annotations

from ..swf_patcher.abc_bytecode import (
    INTEGER_PUSHES,
    PROPERTY_WRITES,
    Instruction,
    Op,
    assemble,
    disassemble,
)
from ..swf_patcher.abc_file import AbcFile
from .swf_edit import Fix, FixKind, FixResult, SwfEdit

CLASS = "red.core.CoreMenu"
OFFSET = "SHOW_ANIM_OFFSET"
HIDE = "hideAnimation"
NUMBER_PUSHES = INTEGER_PUSHES | {Op.PUSHINT, Op.PUSHDOUBLE}
MAIN_TIMELINE = 0   # What the fix changes: the file's main timeline is linked to the menu's class, as character 0


def apply(swf: SwfEdit) -> FixResult:
    if CLASS not in swf.class_names():
        return FixResult([], f"the file has no {CLASS}")
    unit = swf.unit_of_class(CLASS)
    abc = unit.abc
    index, instance = next((i, instance) for i, instance in enumerate(abc.instances)
                           if abc.full_name_of(instance.name) == CLASS)
    hide = next((t.method for t in instance.traits if t.method is not None and abc.name_of(t.name) == HIDE), None)
    bodies = {}
    for method, find_push in ((abc.classes[index].static_init, _offset_push), (hide, _target_push)):
        body = abc.bodies.get(method) if method is not None else None
        if body is None:
            continue
        instructions = disassemble(body.code)
        push = find_push(abc, instructions)
        if push is not None and not _is_zero(instructions[push]):
            bodies[method] = assemble(body.code, instructions, {push: 0}, body.exceptions)
    if not bodies:
        return FixResult([], f"{CLASS} has no slide to remove")
    unit.replace_methods(bodies)
    return FixResult([MAIN_TIMELINE], f"{CLASS} no longer slides: the menu only fades ({len(bodies)} methods changed)")


def _offset_push(abc: AbcFile, instructions: list[Instruction]) -> int | None:
    """The index of the instruction that pushes the value of SHOW_ANIM_OFFSET; None if there is none"""
    for i, ins in enumerate(instructions):
        if ins.opcode in PROPERTY_WRITES and abc.name_of(ins.args[0]) == OFFSET:
            return i - 1 if instructions[i - 1].opcode in NUMBER_PUSHES else None
    return None


def _target_push(abc: AbcFile, instructions: list[Instruction]) -> int | None:
    """The index of the instruction that pushes the y that hideAnimation tweens to; None if there is none"""
    for i, ins in enumerate(instructions[:-1]):
        if ins.opcode is Op.PUSHSTRING and abc.strings[ins.args[0]] == "y" and instructions[i + 1].opcode in NUMBER_PUSHES:
            return i + 1
    return None


def _is_zero(push: Instruction) -> bool:
    return push.opcode in INTEGER_PUSHES and push.args == [0]


FIX = Fix(name="menu-slide-fix",
          kind=FixKind.SMOOTHUI_FEATURE,
          consequence="this menu still slides when it opens and closes",
          apply=apply)
