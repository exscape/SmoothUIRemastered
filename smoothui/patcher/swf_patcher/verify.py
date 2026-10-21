"""Independent checks of a rebuilt SWF body against the original and the plan.

Re-parses the new body and checks that everything the plan did not ask to change is unchanged, and
that the planned changes came out as intended.
"""

from __future__ import annotations

import struct

from ..swf import MalformedSwfError, swf_get_header_offsets
from .abc_bytecode import INTEGER_PUSH_NAMES, PushDouble, disassemble
from .abc_file import parse_abc
from .builder import raw_fps
from .interpolation import change_magnitude, interpolate, simulate_timeline
from .movie import Sprite, SwfMovie
from .place_object import PLACE_OBJECT_CODES, parse_place_object
from .planner import RescalePlan, SpriteTweens
from .retiming import Retiming
from .swf_tags import (
    Tag,
    TagCode,
    parse_sprite_header,
    parse_sprite_tags,
    parse_tags,
    split_do_abc,
)

MAX_SPRITE_PROBLEMS = 12
# How much further (see change_magnitude) an interpolated state may be from either of its keyframes
# than they are from each other: about a pixel, for rounding
MAX_INTERPOLATION_OVERSHOOT = 1.0


def verify(movie: SwfMovie, plan: RescalePlan, new_body: bytes) -> list[str]:
    """Returns a list of problems; empty if all is well"""
    problems = []
    fps_offset, first_tag_offset = swf_get_header_offsets(new_body)
    if first_tag_offset != movie.first_tag_offset:
        problems.append("header length changed")
    if struct.unpack_from("<H", new_body, fps_offset)[0] != raw_fps(plan.target_fps):
        problems.append("fps field wrong")
    if new_body[:fps_offset] != movie.body[:fps_offset]:
        problems.append("stage rect changed")
    if new_body[fps_offset + 2:first_tag_offset] != movie.body[fps_offset + 2:first_tag_offset]:
        problems.append("root frame count changed")

    # Our output, so a parse error is a verification failure, not a malformed input file
    try:
        new_tags = parse_tags(new_body, first_tag_offset)
    except MalformedSwfError as e:
        problems.append(f"new tag list does not parse: {e}")
        return problems
    if len(new_tags) != len(movie.tags):
        problems.append("tag count changed")
        return problems

    new_abc_tag = None
    for i, (old, new) in enumerate(zip(movie.tags, new_tags)):
        if old.code != new.code:
            problems.append(f"tag {i} code changed")
        elif old.code == TagCode.DEFINE_SPRITE:
            character_id = parse_sprite_header(old)[0]
            retiming = plan.sprite_retiming.get(character_id)
            if retiming:
                problems += _verify_stretched_sprite(movie.sprites[character_id], new, retiming,
                                                     plan.tweens.get(character_id))
            elif old.payload != new.payload:
                problems.append(f"unstretched sprite {character_id} changed")
        elif old.code == TagCode.DO_ABC:
            new_abc_tag = new
        elif old != new:
            problems.append(f"tag {i} ({old.code}) changed")

    if new_abc_tag is not None:
        problems += _verify_abc(movie, plan, new_abc_tag)
    return problems


def _split_frames(tags) -> tuple[list[list[Tag]], list[Tag]]:
    """The tags of each frame of a timeline (without the ShowFrames), and the tags after the last
    ShowFrame (normally just End)"""
    frames, current = [], []
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            frames.append(current)
            current = []
        else:
            current.append(tag)
    return frames, current


def _verify_stretched_sprite(sprite: Sprite, new: Tag, retiming: Retiming, tweens: SpriteTweens | None) -> list[str]:
    """(a) the tags of original frame k (labels included) are unchanged, on new frame
       retiming.tick(k), followed only by the planned interpolated moves; the frames in between
       contain only the planned moves;
    (b) a display list simulation of the new timeline matches the original frame's exactly on every
       new frame, except for the planned samples of tweened objects: those are the same objects, with
       the same display options, at their interpolated states, which lie between their two keyframes"""
    cid = sprite.character_id
    try:
        new_sprite_tags = parse_sprite_tags(new)
    except MalformedSwfError as e:
        return [f"sprite {cid}: new tag list does not parse: {e}"]
    old_frames, old_trailing = _split_frames(sprite.tags)
    new_frames, new_trailing = _split_frames(new_sprite_tags)
    if len(new_frames) != retiming.tick(len(old_frames)) or parse_sprite_header(new)[1] != len(new_frames):
        return [f"sprite {cid}: wrong frame count"]
    if new_trailing != old_trailing:
        return [f"sprite {cid}: tags after the last frame changed"]

    samples, moves = (tweens.samples, tweens.moves) if tweens else ({}, {})
    old_states, new_states = simulate_timeline(sprite.tags), simulate_timeline(new_sprite_tags)
    as_pairs = lambda tags: [(tag.code, tag.payload) for tag in tags]
    problems = []
    for k in range(len(old_frames)):
        start = retiming.tick(k)
        for tick in range(start, start + retiming.hold(k)):
            where = f"sprite {cid} f{k} tick {tick - start}"
            original = old_frames[k] if tick == start else []
            planned = moves.get(tick, [])
            if as_pairs(new_frames[tick]) != as_pairs(original + planned):
                problems.append(f"{where}: tags differ from the original frame's and the planned moves")
            if any(tag.code not in PLACE_OBJECT_CODES or not parse_place_object(tag).is_move
                   or parse_place_object(tag).character_id is not None for tag in planned):
                problems.append(f"{where}: a planned move is not a plain move")

            expected = dict(old_states[k])
            for sample in samples.get(tick, []):
                step, obj = sample.step, expected.get(sample.depth)
                if obj is None or obj.instance != step.start.instance or not 0 <= sample.t < 1:
                    problems.append(f"{where} depth {sample.depth}: sample doesn't fit the original")
                    continue
                state = interpolate(step.start, step.end, float(sample.t))
                if max(change_magnitude(step.start, state), change_magnitude(state, step.end)) > \
                        change_magnitude(step.start, step.end) + MAX_INTERPOLATION_OVERSHOOT:
                    problems.append(f"{where} depth {sample.depth}: interpolated state outside its step")
                expected[sample.depth] = obj._replace(matrix=state.matrix, ratio=state.ratio,
                                                      color_transform=state.color_transform)
            if new_states[tick] != expected:
                drifted = sorted(depth for depth in expected.keys() | new_states[tick].keys()
                                 if expected.get(depth) != new_states[tick].get(depth))
                problems.append(f"{where}: state differs from the plan at depths {drifted}")
        if len(problems) > MAX_SPRITE_PROBLEMS:
            break
    return problems


def _verify_abc(movie: SwfMovie, plan: RescalePlan, new_tag: Tag) -> list[str]:
    """Only the planned constants may differ; branches, switches and exception ranges must still
    point at the same instructions"""
    problems = []
    prefix, abc_bytes = split_do_abc(new_tag)
    if prefix != movie.abc_prefix:
        problems.append("DoABC prefix changed")
    try:
        new_abc = parse_abc(abc_bytes)
    except (ValueError, IndexError, struct.error) as e:
        problems.append(f"new ABC does not parse: {e}")
        return problems

    old_abc = movie.abc
    if (old_abc.strings, old_abc.multinames, old_abc.ints, len(old_abc.methods), len(old_abc.bodies)) != \
            (new_abc.strings, new_abc.multinames, new_abc.ints, len(new_abc.methods), len(new_abc.bodies)):
        problems.append("ABC constant pool / method table changed")
    as_bytes = lambda values: [struct.pack("<d", v) for v in values]   # bytewise, as the pool contains a NaN
    if as_bytes(new_abc.doubles) != as_bytes(old_abc.doubles + plan.new_doubles):
        problems.append("double constant pool is not old pool + new constants")

    for method, old_body in old_abc.bodies.items():
        new_body = new_abc.bodies[method]
        if method not in plan.code_edits:
            if (old_body.code, old_body.exceptions) != (new_body.code, new_body.exceptions):
                problems.append(f"method {method} changed unexpectedly")
            continue
        problems += _verify_method(movie, plan, method, new_abc, new_body)
    return problems


def _verify_method(movie, plan, method, new_abc, new_body) -> list[str]:
    old_code, new_code = movie.code[method], disassemble(new_body.code)
    if len(old_code) != len(new_code):
        return [f"method {method} instruction count changed"]

    problems = []
    edits = plan.code_edits[method]
    old_index = {ins.offset: k for k, ins in enumerate(old_code)}
    new_index = {ins.offset: k for k, ins in enumerate(new_code)}
    for k, (old, new) in enumerate(zip(old_code, new_code)):
        if k in edits:
            edit = edits[k]
            if isinstance(edit, PushDouble):
                expected = plan.new_doubles[edit.pool_index - len(movie.abc.doubles)]
                if new.name != "pushdouble" or new_abc.doubles[new.args[0]] != expected:
                    problems.append(f"method {method} ins {k}: patched double wrong")
            elif new.name not in INTEGER_PUSH_NAMES or new.args[0] != edit:
                problems.append(f"method {method} ins {k}: patched value wrong")
        elif old.opcode != new.opcode:
            problems.append(f"method {method} ins {k}: opcode changed")
        elif old.is_branch or old.is_lookupswitch:
            if [old_index[t] for t in old.jump_targets()] != [new_index[t] for t in new.jump_targets()]:
                kind = "branch" if old.is_branch else "switch"
                problems.append(f"method {method} ins {k}: {kind} retargeted")
        elif old.args != new.args:
            problems.append(f"method {method} ins {k}: operands changed")

    remap = lambda exceptions, index: [[index[e[0]], index[e[1]], index[e[2]], e[3], e[4]] for e in exceptions]
    if remap(movie.abc.bodies[method].exceptions, old_index) != remap(new_body.exceptions, new_index):
        problems.append(f"method {method} exception table not equivalent")
    return problems
