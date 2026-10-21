"""In-game options to speed up Gwent.

Adds the class smoothui.gwent.GwentSpeed (gwent_speed_as3/, compiled into gwent_speed.abc with
dev-tools/build-as3.py) as a DoABC tag of its own, and changes eight of the file's methods to use it:

- GwintGameMenu.configUI calls GwentSpeed.install(this) right before it tells the game script that
  the menu is ready (the OnConfigUI call), so that the binding for the options is registered by the
  time the script sends them (see smoothui_gwent.ws)
- CardTweenManager.calcTweenDuration divides the distance a card moves by GwentSpeed.cardSpeed rather
  than by MOVE_TWEEN_SPEED (2000 px per second)
- CardTweenManager.tweenTo, which starts a card's move, then calls GwentSpeed.onCardTween(the move)
  (which puts the card where it goes at once, if moves are instant); or, if the card is where it is to
  go already, GwentSpeed.onCardAtTarget(card, finishCallback) (which ends the instant move that put it
  there, if tweenTo has just stopped it)
- AIPlayerController.state_begin_DelayAction waits GwentSpeed.opponentCardDelay rather than 1200 ms
  (with the card the opponent plays shown on screen) before the opponent plays it
- AIPlayerController.state_begin_SendingCard, which starts moving that card into view, then calls
  GwentSpeed.onOpponentCardSent(this) (which stops it if the card isn't to be shown at all)
- CardFXManager.spawnFX, playRowEffect and spawnFX_Location, which start a card effect (an animation the
  game waits for), then call GwentSpeed.onCardFX(the effect) (which speeds it up or skips it, if the options
  say so)

GwentSpeed shortens the banners itself, at runtime (see GwentSpeed.as). It is injected after
the rescale, so the rescaler never sees this code.

To speed up card effects, CardFXStepper (also in gwent_speed.abc) needs to know which frames of them have
frame scripts, after the rescale: the patcher finds them, and puts them in its ABC in place of a
placeholder string (see _fx_script_frames).
"""

from __future__ import annotations

import struct
from pathlib import Path

from ..swf_patcher.abc_bytecode import NO_OP_NAMES, Instruction, assemble, disassemble
from ..swf_patcher.abc_file import (
    PACKAGE_NAMESPACE,
    AbcFile,
    MethodBody,
    PoolAdditions,
    encode_u30,
    parse_abc,
)
from ..swf_patcher.code_analysis import CodeAnalysis
from ..swf_patcher.movie import SwfMovie
from ..swf_patcher.swf_tags import Tag, TagCode
from .swf_edit import AFTER_RESCALE, CantApply, FixKind, FixResult, SwfEdit

NAME = "gwent-speed-options"
CONSEQUENCE = "Smooth UI's Gwent speed settings do nothing"
KIND = FixKind.SMOOTHUI_FEATURE
PHASE = AFTER_RESCALE

ABC_PATH = Path(__file__).with_name("gwent_speed.abc")
PACKAGE, CLASS = "smoothui.gwent", "GwentSpeed"
MENU_CLASS = "red.game.witcher3.menus.gwint.GwintGameMenu"
TWEEN_CLASS = "red.game.witcher3.menus.gwint.CardTweenManager"
AI_CLASS = "red.game.witcher3.menus.gwint.AIPlayerController"
FX_CLASS = "red.game.witcher3.menus.gwint.CardFXManager"
FX_SPAWNERS = ("spawnFX", "playRowEffect", "spawnFX_Location")
AI_DELAY = 1200     # ms, in AIPlayerController.state_begin_DelayAction
# CardTweenManager.tweenTo(card, targetX, targetY, finishCallback): the local variables of its arguments
TWEEN_TO_ARGUMENTS, TWEEN_TO_CARD, TWEEN_TO_CALLBACK = 4, 1, 4
BANNER_CLASS = "mcStateMessage"     # the banners' W3MessageQueue, for the report
# The string in gwent_speed.abc (CardFXStepper.SCRIPT_FRAMES) that the card effects' frame scripts replace
SCRIPT_FRAMES_PLACEHOLDER = "@SMOOTHUI_FX_SCRIPT_FRAMES@"
# Card effects: the classes CardFXManager starts (the ones whose name starts with this, but those below)
FX_PREFIX = "CardFX"
FX_MANAGER_PREFIX = "CardFXManager"     # CardFXManager and its 1-frame containers
FX_ONGOING_SUFFIX = "_Ongoing"          # weather on the rows, which CardFXManager shows and hides itself
# DoABC flags: kDoAbcLazyInitializeFlag, as Flash sets it
DO_ABC_LAZY = 1

OP_GETLEX, OP_GETLOCAL0, OP_GETLOCAL, OP_GETPROPERTY, OP_CALLPROPVOID = 0x60, 0xD0, 0x62, 0x66, 0x4F


def apply(swf: SwfEdit) -> FixResult:
    abc = swf.abc
    script_frames, not_sped_up = _fx_script_frames(swf)
    our_abc = _with_string(_abc_bytes(), SCRIPT_FRAMES_PLACEHOLDER, script_frames)
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
    on_card_at_target = additions.qname(public, "onCardAtTarget")
    on_card_fx = additions.qname(public, "onCardFX")

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
        # ...; GwentSpeed.onCardTween(curTween); return curTween
        # and if the card is there already: GwentSpeed.onCardAtTarget(card, finishCallback); return null
        tween_to: _hook_tween_to(abc, tween_to,
                                 lambda local: bytes([OP_GETLEX]) + encode_u30(speed_class)
                                 + _getlocal(local) + bytes([OP_CALLPROPVOID])
                                 + encode_u30(on_card_tween) + encode_u30(1),
                                 bytes([OP_GETLEX]) + encode_u30(speed_class)
                                 + _getlocal(TWEEN_TO_CARD) + _getlocal(TWEEN_TO_CALLBACK) + bytes([OP_CALLPROPVOID])
                                 + encode_u30(on_card_at_target) + encode_u30(2)),
        # new Timer(GwentSpeed.opponentCardDelay, 1)
        ai_delay: _replace_ai_delay(abc, ai_delay, bytes([OP_GETLEX]) + encode_u30(speed_class)
                                    + bytes([OP_GETPROPERTY]) + encode_u30(opponent_card_delay)),
        # ...; GwentSpeed.onOpponentCardSent(this)
        ai_sending: _insert_before_return(abc, ai_sending, f"{AI_CLASS}.state_begin_SendingCard", _this,
                                          lambda this: bytes([OP_GETLEX]) + encode_u30(speed_class)
                                          + _getlocal(this) + bytes([OP_CALLPROPVOID])
                                          + encode_u30(on_card_sent) + encode_u30(1)),
    }
    for spawner in FX_SPAWNERS:
        method = _method(abc, FX_CLASS, spawner)
        # ...; GwentSpeed.onCardFX(newCardFX); return ...
        bodies[method] = _insert_before_return(abc, method, f"{FX_CLASS}.{spawner}", _card_fx_local,
                                               lambda local: bytes([OP_GETLEX]) + encode_u30(speed_class)
                                               + _getlocal(local) + bytes([OP_CALLPROPVOID])
                                               + encode_u30(on_card_fx) + encode_u30(1))
    swf.replace_methods(bodies, additions)
    swf.insert_abc(Tag(TagCode.DO_ABC, struct.pack("<I", DO_ABC_LAZY) + PACKAGE.encode() + b"\0" + our_abc,
                       long_header=True))
    return FixResult(banners, f"added {PACKAGE}.{CLASS}; GwintGameMenu.configUI installs it, "
                              "CardTweenManager.calcTweenDuration uses its card speed, "
                              "CardTweenManager.tweenTo tells it about card moves, "
                              f"CardFXManager.{'/'.join(FX_SPAWNERS)} about card effects, "
                              "AIPlayerController.state_begin_DelayAction its opponent card delay, "
                              "AIPlayerController.state_begin_SendingCard tells it about the card"
                              + (f"; can't speed up {', '.join(not_sped_up)} (frame scripts it would skip)"
                                 if not_sped_up else ""))


def _abc_bytes() -> bytes:
    try:
        return ABC_PATH.read_bytes()
    except FileNotFoundError:
        raise CantApply(f"{ABC_PATH.name} is missing; build it with dev-tools/build-as3.py") from None


def _fx_script_frames(swf: SwfEdit) -> tuple[str, list[str]]:
    """The frames of the card effects that have frame scripts, for CardFXStepper, as
    "class=frames;class=frames...": each card effect's class, and the classes in it with frame scripts (named as
    getQualifiedClassName names them), with their 1-based frame numbers, s before those whose script only calls
    stop(). It never steps a card effect onto (or over) a frame with a script, but steps the clips in it along
    with it, so an effect with a clip whose script does more than stop it (or scripts that can't be read) is
    left out: it isn't sped up. Also returns the classes of those."""
    movie = SwfMovie(swf.body())
    scripts = CodeAnalysis(movie).find_frame_scripts()
    entries, left_out = {}, []
    for fx_class, ids in sorted(movie.sprites_of_class.items()):
        if (not fx_class.startswith(FX_PREFIX) or fx_class.startswith(FX_MANAGER_PREFIX)
                or fx_class.endswith(FX_ONGOING_SUFFIX)):
            continue
        classes = {movie.class_name(cid) for cid in _sprite_tree(movie, ids[0])} if len(ids) == 1 else set()
        found = {name: scripts[name] for name in classes if name in scripts}
        if (fx_class not in found or not all(found_scripts.recognised for found_scripts in found.values())
                or any(found_scripts.frames - found_scripts.stop_frames
                       for name, found_scripts in found.items() if name != fx_class)):
            left_out.append(fx_class)
            continue
        for name, found_scripts in found.items():
            stops = found_scripts.stop_frames
            entries[_qualified_name(name)] = ",".join(("s" if frame in stops else "") + str(frame + 1)
                                                      for frame in sorted(found_scripts.frames))
    return ";".join(f"{name}={frames}" for name, frames in sorted(entries.items())), left_out


def _sprite_tree(movie: SwfMovie, character_id: int) -> set[int]:
    """A sprite and the sprites placed in it, all the way down"""
    tree, todo = set(), [character_id]
    while todo:
        sprite = todo.pop()
        if sprite in tree or sprite not in movie.sprites:
            continue
        tree.add(sprite)
        todo.extend(child for child, _ in movie.placements.get(sprite, ()))
    return tree


def _qualified_name(class_name: str) -> str:
    """A class's name as getQualifiedClassName gives it (package::Class)"""
    package, _, name = class_name.rpartition(".")
    return f"{package}::{name}" if package else name


def _with_string(abc_bytes: bytes, old: str, new: str) -> bytes:
    """An ABC file with the string constant old replaced by new. (Nothing in an ABC file refers to a byte
    offset, so what follows the string can move.)"""
    abc = parse_abc(abc_bytes)
    old_entry, new_entry = (encode_u30(len(text.encode())) + text.encode() for text in (old, new))
    pool = abc_bytes[abc.string_pool.values_start:abc.string_pool.end]
    if old not in abc.strings or pool.count(old_entry) != 1:
        raise CantApply(f"{ABC_PATH.name} doesn't have the string {old} (once); rebuild it with dev-tools/build-as3.py")
    result = (abc_bytes[:abc.string_pool.values_start] + pool.replace(old_entry, new_entry)
              + abc_bytes[abc.string_pool.end:])
    if parse_abc(result).strings != [new if text == old else text for text in abc.strings]:
        raise AssertionError(f"BUG: replacing the string {old} in {ABC_PATH.name} went wrong")
    return result


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
    return _insert(body, instructions, {starts[0]: code})


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
    at = _final_return(instructions, description, only_return)
    return _insert(body, instructions, {at: code(find_local(abc, instructions, description))})


def _hook_tween_to(abc: AbcFile, method: int, on_tween, on_at_target: bytes) -> tuple[bytes, list]:
    """CardTweenManager.tweenTo with on_tween(local) inserted before its final statement, `return <the move
    it made>` (see _insert_before_return), and on_at_target before its `return null` for a card that is
    where it is to go already: its only return after it stops the card's move (tryStopCardTween)"""
    description = f"{TWEEN_CLASS}.tweenTo"
    if len(abc.methods[method].param_types) != TWEEN_TO_ARGUMENTS:
        raise CantApply(f"{description}: expected {TWEEN_TO_ARGUMENTS} arguments")
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    final = _final_return(instructions, description, only_return=False)
    stops = [i for i, ins in enumerate(instructions) if _matches(abc, ins, "callpropvoid", "tryStopCardTween")]
    if len(stops) != 1:
        raise CantApply(f"{description}: expected 1 tryStopCardTween call, found {len(stops)}")
    real = [i for i, ins in enumerate(instructions) if ins.name not in NO_OP_NAMES]
    null_returns = [real[k] for k in range(len(real) - 1) if stops[0] < real[k] < final
                    and instructions[real[k]].name == "pushnull" and instructions[real[k + 1]].name == "returnvalue"]
    if len(null_returns) != 1:
        raise CantApply(f"{description}: expected 1 return null after tryStopCardTween, found {len(null_returns)}")
    return _insert(body, instructions, {null_returns[0]: on_at_target,
                                        final: on_tween(_returned_local(abc, instructions, description))})


def _final_return(instructions: list[Instruction], description: str, only_return: bool) -> int:
    """Where a method's final statement starts: a return at its end (its only one, unless only_return is
    false), either a plain return or `return <local variable>`"""
    returns = [i for i, ins in enumerate(instructions) if ins.name in ("returnvoid", "returnvalue")]
    if not returns or returns[-1] != len(instructions) - 1 or (only_return and len(returns) > 1):
        raise CantApply(f"{description}: expected {'1 return' if only_return else 'a return'}, at its end")
    at = len(instructions) - 1
    if instructions[at].name == "returnvalue":
        # Before the statement, not between the value it pushes and the return
        if len(instructions) < 2 or _local_of(instructions[at - 1]) is None:
            raise CantApply(f"{description}: expected it to end with return <local variable>")
        at -= 1
    return at


def _insert(body: MethodBody, instructions: list[Instruction], inserts: dict[int, bytes]) -> tuple[bytes, list]:
    """The method with code inserted before some of its instructions: instruction index -> code"""
    return assemble(body.code, instructions, {at: code + body.code[instructions[at].offset:instructions[at].end]
                                              for at, code in inserts.items()}, body.exceptions)


def _this(abc: AbcFile, instructions: list[Instruction], description: str) -> int:
    """Local variable 0: the object a method is called on"""
    return 0


def _returned_local(abc: AbcFile, instructions: list[Instruction], description: str) -> int:
    """The local variable a method's final `return <local variable>` returns"""
    local = _local_of(instructions[-2]) if len(instructions) >= 2 and instructions[-1].name == "returnvalue" else None
    if local is None:
        raise CantApply(f"{description}: expected it to end with return <local variable>")
    return local


def _card_fx_local(abc: AbcFile, instructions: list[Instruction], description: str) -> int:
    """The local variable a method stores the card effect it makes in (its only `new ...` coerced to CardFX)"""
    made = [instructions[i + 2] for i in range(len(instructions) - 2)
            if instructions[i].name == "construct" and _matches(abc, instructions[i + 1], "coerce", "CardFX")
            and instructions[i + 2].name == "setlocal"]
    if len(made) != 1:
        raise CantApply(f"{description}: expected 1 new card effect stored in a variable, found {len(made)}")
    return made[0].args[0]


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
