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

from pathlib import Path

from ..swf_patcher.abc_bytecode import (
    NO_OPS,
    Instruction,
    Op,
    assemble,
    disassemble,
    encode,
    encode_getlocal,
    find_pattern,
    matches,
    read_local,
)
from ..swf_patcher.abc_file import (
    PACKAGE_NAMESPACE,
    AbcFile,
    MethodBody,
    PoolAdditions,
    encode_u30,
    parse_abc,
)
from ..swf_patcher.code_analysis import MovieAnalysis
from ..swf_patcher.movie import SwfMovie
from .swf_edit import CantApply, Fix, FixKind, FixResult, Phase, SwfEdit, read_abc

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



def apply(swf: SwfEdit) -> FixResult:
    # The hooks' constants go into one ABC's pools, so all the hooked classes must be in that DoABC tag
    unit = swf.unit_of_class(MENU_CLASS)
    for class_name in (TWEEN_CLASS, AI_CLASS, FX_CLASS):
        if swf.unit_of_class(class_name).tag_index != unit.tag_index:
            raise CantApply(f"{class_name} is in another DoABC tag than {MENU_CLASS}")
    abc = unit.abc
    script_frames, not_sped_up = _fx_script_frames(swf)
    our_abc = _with_string(read_abc(ABC_PATH), SCRIPT_FRAMES_PLACEHOLDER, script_frames)
    swf.check_new_classes(our_abc)
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

    config_ui = unit.method(MENU_CLASS, "configUI")
    tween_duration = unit.method(TWEEN_CLASS, "calcTweenDuration")
    tween_to = unit.method(TWEEN_CLASS, "tweenTo")
    ai_delay = unit.method(AI_CLASS, "state_begin_DelayAction")
    ai_sending = unit.method(AI_CLASS, "state_begin_SendingCard")
    bodies = {
        # GwentSpeed.install(this)
        config_ui: _insert_before_config_call(abc, config_ui, encode(Op.GETLEX, speed_class) + encode(Op.GETLOCAL0)
                                              + encode(Op.CALLPROPVOID, install, 1)),
        # ... / GwentSpeed.cardSpeed
        tween_duration: _replace_speed(abc, tween_duration, encode(Op.GETLEX, speed_class)
                                       + encode(Op.GETPROPERTY, card_speed)),
        # ...; GwentSpeed.onCardTween(curTween); return curTween
        # and if the card is there already: GwentSpeed.onCardAtTarget(card, finishCallback); return null
        tween_to: _hook_tween_to(abc, tween_to,
                                 lambda local: encode(Op.GETLEX, speed_class) + encode_getlocal(local)
                                 + encode(Op.CALLPROPVOID, on_card_tween, 1),
                                 encode(Op.GETLEX, speed_class) + encode_getlocal(TWEEN_TO_CARD)
                                 + encode_getlocal(TWEEN_TO_CALLBACK) + encode(Op.CALLPROPVOID, on_card_at_target, 2)),
        # new Timer(GwentSpeed.opponentCardDelay, 1)
        ai_delay: _replace_ai_delay(abc, ai_delay, encode(Op.GETLEX, speed_class)
                                    + encode(Op.GETPROPERTY, opponent_card_delay)),
        # ...; GwentSpeed.onOpponentCardSent(this)
        ai_sending: _insert_before_return(abc, ai_sending, f"{AI_CLASS}.state_begin_SendingCard", _this,
                                          lambda this: encode(Op.GETLEX, speed_class) + encode_getlocal(this)
                                          + encode(Op.CALLPROPVOID, on_card_sent, 1)),
    }
    for spawner in FX_SPAWNERS:
        method = unit.method(FX_CLASS, spawner)
        # ...; GwentSpeed.onCardFX(newCardFX); return ...
        bodies[method] = _insert_before_return(abc, method, f"{FX_CLASS}.{spawner}", _card_fx_local,
                                               lambda local: encode(Op.GETLEX, speed_class) + encode_getlocal(local)
                                               + encode(Op.CALLPROPVOID, on_card_fx, 1))
    unit.replace_methods(bodies, additions)
    swf.insert_abc(PACKAGE, our_abc)
    return FixResult(banners, f"added {PACKAGE}.{CLASS}; GwintGameMenu.configUI installs it, "
                              "CardTweenManager.calcTweenDuration uses its card speed, "
                              "CardTweenManager.tweenTo tells it about card moves, "
                              f"CardFXManager.{'/'.join(FX_SPAWNERS)} about card effects, "
                              "AIPlayerController.state_begin_DelayAction its opponent card delay, "
                              "AIPlayerController.state_begin_SendingCard tells it about the card"
                              + (f"; can't speed up {', '.join(not_sped_up)} (frame scripts it would skip)"
                                 if not_sped_up else ""))


def _fx_script_frames(swf: SwfEdit) -> tuple[str, list[str]]:
    """The frames of the card effects that have frame scripts, for CardFXStepper, as
    "class=frames;class=frames...": each card effect's class, and the classes in it with frame scripts (named as
    getQualifiedClassName names them), with their 1-based frame numbers, s before those whose script only calls
    stop(). It never steps a card effect onto (or over) a frame with a script, but steps the clips in it along
    with it, so an effect with a clip whose script does more than stop it (or scripts that can't be read) is
    left out: it isn't sped up. Also returns the classes of those."""
    movie = SwfMovie(swf.body())
    scripts = MovieAnalysis(movie).find_frame_scripts()
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


def _insert_before_config_call(abc: AbcFile, method: int, code: bytes) -> tuple[bytes, list]:
    """The method with code inserted before its statement dispatchEvent(new GameEvent(GameEvent.CALL,
    "OnConfigUI"))"""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    starts = [run[0] for run in find_pattern(abc, instructions, [
        (Op.FINDPROPSTRICT, "dispatchEvent"), (Op.FINDPROPSTRICT, "GameEvent"), (Op.GETLEX, "GameEvent"),
        (Op.GETPROPERTY, "CALL"), (Op.PUSHSTRING, "OnConfigUI")], skip_no_ops=False)]
    if len(starts) != 1:
        raise CantApply(f"{MENU_CLASS}.configUI: expected 1 OnConfigUI call, found {len(starts)}")
    return _insert(body, instructions, {starts[0]: code})


def _replace_speed(abc: AbcFile, method: int, code: bytes) -> tuple[bytes, list]:
    """The method with its `getlex MOVE_TWEEN_SPEED` (the divisor of distance / MOVE_TWEEN_SPEED) replaced"""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    found = [i for i, ins in enumerate(instructions[:-1])
             if matches(abc, ins, Op.GETLEX, "MOVE_TWEEN_SPEED") and instructions[i + 1].opcode is Op.DIVIDE]
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
    stops = [i for i, ins in enumerate(instructions) if matches(abc, ins, Op.CALLPROPVOID, "tryStopCardTween")]
    if len(stops) != 1:
        raise CantApply(f"{description}: expected 1 tryStopCardTween call, found {len(stops)}")
    real = [i for i, ins in enumerate(instructions) if ins.opcode not in NO_OPS]
    null_returns = [real[k] for k in range(len(real) - 1) if stops[0] < real[k] < final
                    and instructions[real[k]].opcode is Op.PUSHNULL and instructions[real[k + 1]].opcode is Op.RETURNVALUE]
    if len(null_returns) != 1:
        raise CantApply(f"{description}: expected 1 return null after tryStopCardTween, found {len(null_returns)}")
    return _insert(body, instructions, {null_returns[0]: on_at_target,
                                        final: on_tween(_returned_local(abc, instructions, description))})


def _final_return(instructions: list[Instruction], description: str, only_return: bool) -> int:
    """Where a method's final statement starts: a return at its end (its only one, unless only_return is
    false), either a plain return or `return <local variable>`"""
    returns = [i for i, ins in enumerate(instructions) if ins.opcode in (Op.RETURNVOID, Op.RETURNVALUE)]
    if not returns or returns[-1] != len(instructions) - 1 or (only_return and len(returns) > 1):
        raise CantApply(f"{description}: expected {'1 return' if only_return else 'a return'}, at its end")
    at = len(instructions) - 1
    if instructions[at].opcode is Op.RETURNVALUE:
        # Before the statement, not between the value it pushes and the return
        if len(instructions) < 2 or read_local(instructions[at - 1]) is None:
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
    local = read_local(instructions[-2]) if len(instructions) >= 2 and instructions[-1].opcode is Op.RETURNVALUE else None
    if local is None:
        raise CantApply(f"{description}: expected it to end with return <local variable>")
    return local


def _card_fx_local(abc: AbcFile, instructions: list[Instruction], description: str) -> int:
    """The local variable a method stores the card effect it makes in (its only `new ...` coerced to CardFX)"""
    made = [instructions[i + 2] for i in range(len(instructions) - 2)
            if instructions[i].opcode is Op.CONSTRUCT and matches(abc, instructions[i + 1], Op.COERCE, "CardFX")
            and instructions[i + 2].opcode is Op.SETLOCAL]
    if len(made) != 1:
        raise CantApply(f"{description}: expected 1 new card effect stored in a variable, found {len(made)}")
    return made[0].args[0]


def _replace_ai_delay(abc: AbcFile, method: int, code: bytes) -> tuple[bytes, list]:
    """The method with the delay of its `new Timer(1200, 1)` replaced"""
    body = abc.bodies[method]
    instructions = disassemble(body.code)
    found = [i for i in range(1, len(instructions) - 2)
             if matches(abc, instructions[i - 1], Op.FINDPROPSTRICT, "Timer")
             and instructions[i].opcode is Op.PUSHSHORT and instructions[i].args == [AI_DELAY]
             and instructions[i + 1].opcode is Op.PUSHBYTE and instructions[i + 1].args == [1]
             and matches(abc, instructions[i + 2], Op.CONSTRUCTPROP, "Timer") and instructions[i + 2].args[1] == 2]
    if len(found) != 1:
        raise CantApply(f"{AI_CLASS}.state_begin_DelayAction: expected 1 new Timer({AI_DELAY}, 1), found {len(found)}")
    return assemble(body.code, instructions, {found[0]: code}, body.exceptions)


FIX = Fix(name="gwent-speed-options",
          kind=FixKind.SMOOTHUI_FEATURE,
          consequence="Smooth UI's Gwent speed settings do nothing",
          apply=apply,
          phase=Phase.AFTER_RESCALE)
