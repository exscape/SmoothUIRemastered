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
    NO_OP_NAMES,
    Instruction,
    assemble,
    branch_targets,
    disassemble,
)
from ..swf_patcher.abc_file import AbcFile
from .swf_edit import CantApply, Fix, FixKind, FixResult, Phase, SwfEdit

CLASS = "red.game.witcher3.menus.overlay.BookPopup"
METHOD = "populateSingleBookData"
FADE_END = "onTitleHidden"


def apply(swf: SwfEdit) -> FixResult:
    unit = swf.unit_of_class(CLASS)
    abc = unit.abc
    instance = next(i for i in abc.instances if abc.full_name_of(i.name) == CLASS)
    method = next((t.method for t in instance.traits if t.method is not None and abc.name_of(t.name) == METHOD), None)
    if method is None or method not in abc.bodies:
        raise CantApply(f"{CLASS} has no method {METHOD}")
    body = abc.bodies[method]
    instructions = disassemble(body.code)

    fade = _find_fade(abc, instructions)
    if fade is None:
        if not any(ins.name == "getproperty" and abc.name_of(ins.args[0]) == FADE_END for ins in instructions):
            return FixResult([], f"{CLASS}.{METHOD} doesn't fade")
        raise CantApply(f"{CLASS}.{METHOD} doesn't fade in an `if (!_isFirstInit)` branch")
    first, last = fade
    inner = {ins.offset for ins in instructions[first + 1:last + 1]}
    if inner & branch_targets(instructions):
        raise CantApply(f"{METHOD} has a branch into its fade")
    edits = {i: b"" for i in range(first, last + 1)}
    unit.replace_method_code(method, *assemble(body.code, instructions, edits, body.exceptions))
    return FixResult(swf.sprites_of_class_or_subclasses(abc, instance.name),
                     f"{METHOD} shows every book at once, without fading the title and text out and back in")


def _find_fade(abc: AbcFile, instructions: list[Instruction]) -> tuple[int, int] | None:
    """(first, last instruction index) of the fade in `if (!_isFirstInit) {fade} else {...}`: from the getlocal0
    of the condition on to the jump over the else branch; None if there is no such branch with a tween that calls
    onTitleHidden. Debug instructions are allowed."""
    real = [i for i, ins in enumerate(instructions) if ins.name not in NO_OP_NAMES]
    index_at = {ins.offset: i for i, ins in enumerate(instructions)}
    pattern = [("getlocal0", None), ("getproperty", "_isFirstInit"), ("iftrue", None)]
    for k in range(len(real) - len(pattern) + 1):
        window = [instructions[i] for i in real[k:k + len(pattern)]]
        if not all(ins.name == name and (prop is None or abc.name_of(ins.args[0]) == prop)
                   for ins, (name, prop) in zip(window, pattern)):
            continue
        branch = window[-1]
        else_start = index_at.get(branch.end + branch.args[0])
        if else_start is None or else_start <= real[k + 2]:
            return None
        jump = max((i for i in real if real[k + 2] < i < else_start), default=None)
        if jump is None or instructions[jump].name != "jump":
            return None
        fade = instructions[real[k + 2] + 1:jump]
        if not any(ins.name == "getproperty" and abc.name_of(ins.args[0]) == FADE_END for ins in fade):
            return None
        return real[k], jump
    return None


FIX = Fix(name="book-fade-fix",
          kind=FixKind.GAME_BUG,
          consequence="in books read from the inventory, the text fades out and back in when opening and switching between books",
          apply=apply,
          # Code only: the rescale needn't see it
          phase=Phase.AFTER_RESCALE)
