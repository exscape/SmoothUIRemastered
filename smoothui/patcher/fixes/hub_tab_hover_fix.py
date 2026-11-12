"""panel_common: hovering over the hub's tabs leaves several of them lit.

The tab bar at the top of the menus (TopBarNew) lights a tab while the mouse is over it, and unlights it
when the mouse leaves, unless it is the selected tab. It decides that by comparing the tab's menu id with
the selected one. But tabs that open different states of one menu (the blacksmith's Repair, Sockets
and Disassemble tabs, for example) share their id, and only differ in their state. So when one of them is
selected, all the others with its id count as selected too, and once the mouse has been over them they
stay lit, until a click selects a tab and resets them all. (setSelectedTab, the one that does select
tabs, does compare the state too.)

The fix compares with the list's selected tab instead: setSelectedTab picks it by id and state, and
the arrow keys and bumpers move it too.
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
from ..swf_patcher.abc_file import AbcFile
from .swf_edit import CantApply, Fix, FixKind, FixResult, SwfEdit, replace_run

CLASS = "red.game.witcher3.menus.common_menu.TopBarNew"
METHOD = "onTabItemMouseOut"



def apply(swf: SwfEdit) -> FixResult:
    unit = swf.unit_of_class(CLASS)
    abc = unit.abc
    _, instance = unit.instance(CLASS)
    method = unit.method(CLASS, METHOD)
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    compare = _find_id_comparison(abc, instructions)
    if compare is None:
        if _selected_comparison_exists(abc, instructions):
            return FixResult([], f"{CLASS}.{METHOD} already compares with the selected tab")
        raise CantApply(f"{CLASS}.{METHOD} doesn't compare the hovered tab's id with the selected id")
    first, last = compare    # instructions[first:last + 1]: _lastMouseOveredItem's data.id == cachedSelectedId

    tab_list = _find_multiname(abc, instance, Op.GETPROPERTY, "mcTabList")
    get_selected = _find_multiname(abc, instance, Op.CALLPROPERTY, "getSelectedRenderer")
    if tab_list is None or get_selected is None:
        raise CantApply(f"{CLASS} doesn't use mcTabList.getSelectedRenderer() anywhere")

    # ... _lastMouseOveredItem == this.mcTabList.getSelectedRenderer()
    replacement = (encode(Op.GETLOCAL0) + encode(Op.GETPROPERTY, tab_list) + encode(Op.CALLPROPERTY, get_selected, 0)
                   + encode(instructions[last].opcode))
    edits = replace_run(instructions, first, last, replacement, METHOD, "the id comparison")
    code, exceptions = assemble(body.code, instructions, edits, body.exceptions)
    unit.replace_method_code(method, code, exceptions)
    return FixResult(swf.sprites_of_class_or_subclasses(abc, instance.name),
                     f"{METHOD} now unlights the hovered tab unless it is the list's selected one, "
                     "not any tab with the selected id")


def _find_id_comparison(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int] | None:
    """(first, last) instruction indices of `<tab>.data.id == this.cachedSelectedId`, from the
    getproperty data on to the equals; None if the method doesn't contain it"""
    found = find_pattern(abc, instructions, [(Op.GETPROPERTY, "data"), (Op.GETPROPERTY, "id"), (Op.GETLOCAL0, None),
                                             (Op.GETPROPERTY, "cachedSelectedId"), (Op.EQUALS, None)],
                         skip_no_ops=False)
    return (found[0][0], found[0][-1]) if found else None


def _selected_comparison_exists(abc: AbcFile, instructions: list[Instruction]) -> bool:
    return any(ins.opcode is Op.CALLPROPERTY and abc.name_of(ins.args[0]) == "getSelectedRenderer"
               for ins in instructions)


def _find_multiname(abc: AbcFile, instance, opcode: Op, name: str) -> int | None:
    """The multiname index that the class's own methods use for this property access"""
    for trait in instance.traits:
        body = abc.bodies.get(trait.method) if trait.method is not None else None
        for ins in disassemble(body.code) if body else ():
            if ins.opcode is opcode and abc.name_of(ins.args[0]) == name and ins.args[1:] in ([], [0]):
                return ins.args[0]
    return None


FIX = Fix(name="hub-tab-hover-fix",
          kind=FixKind.GAME_BUG,
          consequence="in the tab bar at the top of some menus, several tabs can be highlighted at once",
          apply=apply)
