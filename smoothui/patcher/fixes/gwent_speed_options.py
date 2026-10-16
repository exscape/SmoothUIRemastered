"""In-game options to speed up Gwent.

Adds the class smoothui.gwent.GwentSpeed (gwent_speed_as3/, compiled into gwent_speed.abc with
dev-tools/build-as3.py) as a DoABC tag of its own, and changes five of the file's methods to use it:

- GwintGameMenu.configUI calls GwentSpeed.install(this) right before it tells the game script that
  the menu is ready (the OnConfigUI call), so that the binding for the options is registered by the
  time the script sends them (see smoothui_gwent.ws)
- CardTweenManager.calcTweenDuration divides the distance a card moves by GwentSpeed.cardSpeed rather
  than by MOVE_TWEEN_SPEED (2000 px per second)
- CardTweenManager.tweenTo, which starts a card's move, then calls GwentSpeed.onCardTween(the move)
  (which puts the card where it goes at once, if moves are instant)
- AIPlayerController.state_begin_DelayAction waits GwentSpeed.opponentCardDelay rather than 1200 ms
  (with the card the opponent plays shown on screen) before the opponent plays it
- AIPlayerController.state_begin_SendingCard, which starts moving that card into view, then calls
  GwentSpeed.onOpponentCardSent(this) (which stops it if the card isn't to be shown at all)

GwentSpeed shortens the banners itself, at runtime (see GwentSpeed.as). It is injected after
the rescale, so the rescaler never sees this code.
"""

from __future__ import annotations

import struct
from pathlib import Path

from ..swf_patcher.abc_bytecode import Instruction, assemble, disassemble
from ..swf_patcher.abc_file import (
    PACKAGE_NAMESPACE,
    AbcFile,
    PoolAdditions,
    encode_u30,
    parse_abc,
)
from ..swf_patcher.swf_tags import Tag, TagCode
from .swf_edit import AFTER_RESCALE, CantApply, FixResult, SwfEdit

NAME = "gwent-speed-options"
PHASE = AFTER_RESCALE

ABC_PATH = Path(__file__).with_name("gwent_speed.abc")
PACKAGE, CLASS = "smoothui.gwent", "GwentSpeed"
MENU_CLASS = "red.game.witcher3.menus.gwint.GwintGameMenu"
TWEEN_CLASS = "red.game.witcher3.menus.gwint.CardTweenManager"
AI_CLASS = "red.game.witcher3.menus.gwint.AIPlayerController"
AI_DELAY = 1200     # ms, in AIPlayerController.state_begin_DelayAction
BANNER_CLASS = "mcStateMessage"     # the banners' W3MessageQueue, for the report
# DoABC flags: kDoAbcLazyInitializeFlag, as Flash sets it
DO_ABC_LAZY = 1

OP_GETLEX, OP_GETLOCAL0, OP_GETLOCAL, OP_GETPROPERTY, OP_CALLPROPVOID = 0x60, 0xD0, 0x62, 0x66, 0x4F


def apply(swf: SwfEdit) -> FixResult:
    abc = swf.abc
    our_abc = _abc_bytes()
    ours = parse_abc(our_abc)
    defined = {abc.full_name_of(instance.name) for instance in abc.instances}
    if clash := defined & {ours.full_name_of(instance.name) for instance in ours.instances}:
        raise CantApply(f"the file already defines {', '.join(sorted(clash))}")
    banners = swf.sprites_of_class(BANNER_CLASS)
    if len(banners) != 1:
        raise CantApply(f"expected 1 sprite linked to {BANNER_CLASS}, found {len(banners)}")

    additions = PoolAdditions(abc)
    public = additions.namespace(PACKAGE_NAMESPACE, "")
    speed_class = additions.qname(additions.namespace(PACKAGE_NAMESPACE, PACKAGE), CLASS)
    install = additions.qname(public, "install")
    card_speed = additions.qname(public, "cardSpeed")
    opponent_card_delay = additions.qname(public, "opponentCardDelay")
    on_card_sent = additions.qname(public, "onOpponentCardSent")
    on_card_tween = additions.qname(public, "onCardTween")

    config_ui = _method(abc, MENU_CLASS, "configUI")
    tween_duration = _method(abc, TWEEN_CLASS, "calcTweenDuration")
    tween_to = _method(abc, TWEEN_CLASS, "tweenTo")
    ai_delay = _method(abc, AI_CLASS, "state_begin_DelayAction")
    ai_sending = _method(abc, AI_CLASS, "state_begin_SendingCard")
    bodies = {
        # GwentSpeed.install(this)
        config_ui: _insert_before_config_call(abc, config_ui, bytes([OP_GETLEX]) + encode_u30(speed_class)
                                              + bytes([OP_GETLOCAL0, OP_CALLPROPVOID]) + encode_u30(install)
                                              + encode_u30(1)),
        # ... / GwentSpeed.cardSpeed
        tween_duration: _replace_speed(abc, tween_duration, bytes([OP_GETLEX]) + encode_u30(speed_class)
                                       + bytes([OP_GETPROPERTY]) + encode_u30(card_speed)),
        # ...; GwentSpeed.onCardTween(curTween); return curTween (its earlier returns make no move)
        tween_to: _insert_before_return(abc, tween_to, f"{TWEEN_CLASS}.tweenTo", _returned_local,
                                        lambda local: bytes([OP_GETLEX]) + encode_u30(speed_class)
                                        + _getlocal(local) + bytes([OP_CALLPROPVOID])
                                        + encode_u30(on_card_tween) + encode_u30(1), only_return=False),
        # new Timer(GwentSpeed.opponentCardDelay, 1)
        ai_delay: _replace_ai_delay(abc, ai_delay, bytes([OP_GETLEX]) + encode_u30(speed_class)
                                    + bytes([OP_GETPROPERTY]) + encode_u30(opponent_card_delay)),
        # ...; GwentSpeed.onOpponentCardSent(this)
        ai_sending: _insert_before_return(abc, ai_sending, f"{AI_CLASS}.state_begin_SendingCard", _this,
                                          lambda this: bytes([OP_GETLEX]) + encode_u30(speed_class)
                                          + _getlocal(this) + bytes([OP_CALLPROPVOID])
                                          + encode_u30(on_card_sent) + encode_u30(1)),
    }
    swf.replace_methods(bodies, additions)
    swf.insert_abc(Tag(TagCode.DO_ABC, struct.pack("<I", DO_ABC_LAZY) + PACKAGE.encode() + b"\0" + our_abc,
                       long_header=True))
    return FixResult(banners, f"added {PACKAGE}.{CLASS}; GwintGameMenu.configUI installs it, "
                              "CardTweenManager.calcTweenDuration uses its card speed, "
                              "CardTweenManager.tweenTo tells it about card moves, "
                              "AIPlayerController.state_begin_DelayAction its opponent card delay, "
                              "AIPlayerController.state_begin_SendingCard tells it about the card")


def _abc_bytes() -> bytes:
    try:
        return ABC_PATH.read_bytes()
    except FileNotFoundError:
        raise CantApply(f"{ABC_PATH.name} is missing; build it with dev-tools/build-as3.py") from None


def _method(abc: AbcFile, class_name: str, method_name: str) -> int:
    instance = next((i for i in abc.instances if abc.full_name_of(i.name) == class_name), None)
    if instance is None:
        raise CantApply(f"found no class {class_name}")
    method = next((t.method for t in instance.traits
                   if t.method is not None and abc.name_of(t.name) == method_name), None)
    if method is None or method not in abc.bodies:
        raise CantApply(f"{class_name} has no method {method_name}")
    return method


def _insert_before_config_call(abc: AbcFile, method: int, code: bytes) -> tuple[bytes, list]:
    """The method with code inserted before its statement dispatchEvent(new GameEvent(GameEvent.CALL,
    "OnConfigUI"))"""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    pattern = [("findpropstrict", "dispatchEvent"), ("findpropstrict", "GameEvent"), ("getlex", "GameEvent"),
               ("getproperty", "CALL"), ("pushstring", "OnConfigUI")]
    starts = [start for start in range(len(instructions) - len(pattern) + 1)
              if all(_matches(abc, ins, name, value) for ins, (name, value)
                     in zip(instructions[start:start + len(pattern)], pattern))]
    if len(starts) != 1:
        raise CantApply(f"{MENU_CLASS}.configUI: expected 1 OnConfigUI call, found {len(starts)}")
    start = starts[0]
    first = instructions[start]
    return assemble(body.code, instructions, {start: code + body.code[first.offset:first.end]}, body.exceptions)


def _replace_speed(abc: AbcFile, method: int, code: bytes) -> tuple[bytes, list]:
    """The method with its `getlex MOVE_TWEEN_SPEED` (the divisor of distance / MOVE_TWEEN_SPEED) replaced"""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    found = [i for i, ins in enumerate(instructions[:-1])
             if _matches(abc, ins, "getlex", "MOVE_TWEEN_SPEED") and instructions[i + 1].name == "divide"]
    if len(found) != 1:
        raise CantApply(f"{TWEEN_CLASS}.calcTweenDuration: expected 1 division by MOVE_TWEEN_SPEED, found {len(found)}")
    return assemble(body.code, instructions, {found[0]: code}, body.exceptions)


def _insert_before_return(abc: AbcFile, method: int, description: str, find_local, code,
                          only_return: bool = True) -> tuple[bytes, list]:
    """The method with code(local) inserted before its final statement: a return at its end (its only one,
    unless only_return is false), either a plain return or `return <local variable>`. find_local(abc,
    instructions, description) picks the local variable (register) code reads. Only adds what code pushes
    to the stack: it must fit the method's max_stack, which stays as it is."""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    returns = [i for i, ins in enumerate(instructions) if ins.name in ("returnvoid", "returnvalue")]
    if not returns or returns[-1] != len(instructions) - 1 or (only_return and len(returns) > 1):
        raise CantApply(f"{description}: expected {'1 return' if only_return else 'a return'}, at its end")
    at = len(instructions) - 1
    if instructions[at].name == "returnvalue":
        # Before the statement, not between the value it pushes and the return
        if len(instructions) < 2 or _local_of(instructions[at - 1]) is None:
            raise CantApply(f"{description}: expected it to end with return <local variable>")
        at -= 1
    local = find_local(abc, instructions, description)
    first = instructions[at]
    return assemble(body.code, instructions, {at: code(local) + body.code[first.offset:first.end]}, body.exceptions)


def _this(abc: AbcFile, instructions: list[Instruction], description: str) -> int:
    """Local variable 0: the object a method is called on"""
    return 0


def _returned_local(abc: AbcFile, instructions: list[Instruction], description: str) -> int:
    """The local variable a method's final `return <local variable>` returns"""
    local = _local_of(instructions[-2]) if len(instructions) >= 2 and instructions[-1].name == "returnvalue" else None
    if local is None:
        raise CantApply(f"{description}: expected it to end with return <local variable>")
    return local


def _local_of(ins: Instruction) -> int | None:
    """The local variable (register) a getlocal instruction reads"""
    if ins.name == "getlocal":
        return ins.args[0]
    if ins.name in ("getlocal0", "getlocal1", "getlocal2", "getlocal3"):
        return int(ins.name[-1])
    return None


def _getlocal(local: int) -> bytes:
    return bytes([OP_GETLOCAL0 + local]) if local <= 3 else bytes([OP_GETLOCAL]) + encode_u30(local)


def _replace_ai_delay(abc: AbcFile, method: int, code: bytes) -> tuple[bytes, list]:
    """The method with the delay of its `new Timer(1200, 1)` replaced"""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    found = [i for i in range(1, len(instructions) - 2)
             if _matches(abc, instructions[i - 1], "findpropstrict", "Timer")
             and instructions[i].name == "pushshort" and instructions[i].args == [AI_DELAY]
             and instructions[i + 1].name == "pushbyte" and instructions[i + 1].args == [1]
             and _matches(abc, instructions[i + 2], "constructprop", "Timer") and instructions[i + 2].args[1] == 2]
    if len(found) != 1:
        raise CantApply(f"{AI_CLASS}.state_begin_DelayAction: expected 1 new Timer({AI_DELAY}, 1), found {len(found)}")
    return assemble(body.code, instructions, {found[0]: code}, body.exceptions)


def _matches(abc: AbcFile, ins: Instruction, name: str, value: str) -> bool:
    if ins.name != name:
        return False
    if name == "pushstring":
        return abc.strings[ins.args[0]] == value
    return abc.name_of(ins.args[0]) == value
