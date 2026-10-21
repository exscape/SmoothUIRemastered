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
    assemble,
    branch_targets,
    disassemble,
)
from ..swf_patcher.abc_file import AbcFile, encode_u30
from .swf_edit import CantApply, FixKind, FixResult, SwfEdit

NAME = "hub-tab-hover-fix"
CONSEQUENCE = "in the tab bar at the top of some menus, several tabs can be highlighted at once"
KIND = FixKind.GAME_BUG
CLASS = "red.game.witcher3.menus.common_menu.TopBarNew"
METHOD = "onTabItemMouseOut"

OP_GETLOCAL0, OP_GETPROPERTY, OP_CALLPROPERTY = 0xD0, 0x66, 0x46


def apply(swf: SwfEdit) -> FixResult:
    abc = swf.abc
    instance = next((i for i in abc.instances if abc.full_name_of(i.name) == CLASS), None)
    if instance is None:
        raise CantApply(f"found no class {CLASS}")
    method = next((t.method for t in instance.traits if t.method is not None and abc.name_of(t.name) == METHOD), None)
    if method is None or method not in abc.bodies:
        raise CantApply(f"{CLASS} has no method {METHOD}")
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    compare = _find_id_comparison(abc, instructions)
    if compare is None:
        if _selected_comparison_exists(abc, instructions):
            return FixResult([], f"{CLASS}.{METHOD} already compares with the selected tab")
        raise CantApply(f"{CLASS}.{METHOD} doesn't compare the hovered tab's id with the selected id")
    first, last = compare    # instructions[first:last + 1]: _lastMouseOveredItem's data.id == cachedSelectedId

    tab_list = _find_multiname(abc, instance, OP_GETPROPERTY, "mcTabList")
    get_selected = _find_multiname(abc, instance, OP_CALLPROPERTY, "getSelectedRenderer")
    if tab_list is None or get_selected is None:
        raise CantApply(f"{CLASS} doesn't use mcTabList.getSelectedRenderer() anywhere")

    # ... _lastMouseOveredItem == this.mcTabList.getSelectedRenderer()
    replacement = (bytes([OP_GETLOCAL0]) + bytes([OP_GETPROPERTY]) + encode_u30(tab_list)
                   + bytes([OP_CALLPROPERTY]) + encode_u30(get_selected) + encode_u30(0)
                   + bytes([instructions[last].opcode]))
    inner = {ins.offset for ins in instructions[first + 1:last + 1]}
    if inner & branch_targets(instructions):
        raise CantApply(f"{METHOD} has a branch into the id comparison")
    edits = {first: replacement, **{i: b"" for i in range(first + 1, last + 1)}}
    code, exceptions = assemble(body.code, instructions, edits, body.exceptions)
    swf.replace_method_code(method, code, exceptions)
    return FixResult(_sprites_using(swf, abc, instance.name),
                     f"{METHOD} now unlights the hovered tab unless it is the list's selected one, "
                     "not any tab with the selected id")


def _find_id_comparison(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int] | None:
    """(first, last) instruction indices of `<tab>.data.id == this.cachedSelectedId`, from the
    getproperty data on to the equals; None if the method doesn't contain it"""
    pattern = [("getproperty", "data"), ("getproperty", "id"), ("getlocal0", None),
               ("getproperty", "cachedSelectedId"), ("equals", None)]
    for start in range(len(instructions) - len(pattern) + 1):
        window = instructions[start:start + len(pattern)]
        if all(ins.name == name and (prop is None or abc.name_of(ins.args[0]) == prop)
               for ins, (name, prop) in zip(window, pattern)):
            return start, start + len(pattern) - 1
    return None


def _selected_comparison_exists(abc: AbcFile, instructions: list[Instruction]) -> bool:
    return any(ins.name == "callproperty" and abc.name_of(ins.args[0]) == "getSelectedRenderer"
               for ins in instructions)


def _find_multiname(abc: AbcFile, instance, opcode: int, name: str) -> int | None:
    """The multiname index that the class's own methods use for this property access"""
    for trait in instance.traits:
        body = abc.bodies.get(trait.method) if trait.method is not None else None
        for ins in disassemble(body.code) if body else ():
            if ins.opcode == opcode and abc.name_of(ins.args[0]) == name and ins.args[1:] in ([], [0]):
                return ins.args[0]
    return None


def _sprites_using(swf: SwfEdit, abc: AbcFile, class_name_index: int) -> list[int]:
    """Ids of the sprites linked to the class or to a class that extends it"""
    names = {class_name_index}
    grew = True
    while grew:
        grew = False
        for instance in abc.instances:
            if instance.super_name in names and instance.name not in names:
                names.add(instance.name)
                grew = True
    return sorted({sprite for name in names for sprite in swf.sprites_of_class(abc.full_name_of(name))})
