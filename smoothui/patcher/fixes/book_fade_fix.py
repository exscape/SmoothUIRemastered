"""Books: the text fades out and back in when switching books, and as a book opens from the inventory while it holds
other unread books.

With other unread books in the inventory, the book popup lists them in a row above the text, to switch between them.
BookPopup's populateSingleBookData shows a book: a popup's first book at once, any other with a fade, the title and
text out and back in. But it puts the new text in before the fade out (only the title waits for it), so the new
text fades out and back in. And on opening, the list reports its first selection a frame later, so the first book
is shown again, with that fade.

The fix drops the fade: every book shows at once, as a popup's first one did (as the Glossary's Books do).
"""

from __future__ import annotations

from ..swf_patcher.abc_bytecode import (
    NO_OPS,
    Instruction,
    Op,
    assemble,
    disassemble,
    find_pattern,
)
from ..swf_patcher.abc_file import AbcFile
from .swf_edit import CantApply, Fix, FixKind, FixResult, Phase, SwfEdit, replace_run

CLASS = "red.game.witcher3.menus.overlay.BookPopup"
METHOD = "populateSingleBookData"
FADE_END = "onTitleHidden"


def apply(swf: SwfEdit) -> FixResult:
    unit = swf.unit_of_class(CLASS)
    abc = unit.abc
    _, instance = unit.instance(CLASS)
    method = unit.method(CLASS, METHOD)
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    fade = _find_fade(abc, instructions)
    if fade is None:
        if not any(ins.opcode is Op.GETPROPERTY and abc.name_of(ins.args[0]) == FADE_END for ins in instructions):
            return FixResult([], f"{CLASS}.{METHOD} doesn't fade")
        raise CantApply(f"{CLASS}.{METHOD} doesn't fade in an `if (!_isFirstInit)` branch")
    edits = replace_run(instructions, *fade, b"", METHOD, "its fade")
    unit.replace_method_code(method, *assemble(body.code, instructions, edits, body.exceptions))
    return FixResult(swf.sprites_of_class_or_subclasses(abc, instance.name),
                     f"{METHOD} shows every book at once, without fading the title and text out and back in")


def _find_fade(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int] | None:
    """(first, last instruction index) of the fade in `if (!_isFirstInit) {fade} else {...}`: from the getlocal0
    of the condition on to the jump over the else branch; None if there is no such branch with a tween that calls
    onTitleHidden. Debug instructions are allowed."""
    found = find_pattern(abc, instructions, [(Op.GETLOCAL0, None), (Op.GETPROPERTY, "_isFirstInit"), (Op.IFTRUE, None)],
                         skip_no_ops=True)
    if not found:
        return None
    start, _, at_branch = found[0]
    branch = instructions[at_branch]
    else_start = {ins.offset: i for i, ins in enumerate(instructions)}.get(branch.end + branch.args[0])
    if else_start is None or else_start <= at_branch:
        return None
    jump = max((i for i, ins in enumerate(instructions) if at_branch < i < else_start and ins.opcode not in NO_OPS),
               default=None)
    if jump is None or instructions[jump].opcode is not Op.JUMP:
        return None
    fade = instructions[at_branch + 1:jump]
    if not any(ins.opcode is Op.GETPROPERTY and abc.name_of(ins.args[0]) == FADE_END for ins in fade):
        return None
    return start, jump


FIX = Fix(name="book-fade-fix",
          kind=FixKind.GAME_BUG,
          consequence="in books read from the inventory, the text fades out and back in when opening and switching between books",
          apply=apply,
          # Code only: the rescale needn't see it
          phase=Phase.AFTER_RESCALE)
