"""Independent checks of a rebuilt SWF body against the original and the plan.

Re-parses the new body and checks that everything the plan did not ask to change is unchanged, and
that the planned changes came out as intended.
"""

from __future__ import annotations

import struct

from ..swf import swf_get_header_offsets
from .abc_bytecode import INTEGER_PUSH_NAMES, PushDouble, disassemble
from .abc_file import parse_abc
from .builder import raw_fps
from .interpolation import simulate_timeline
from .movie import Sprite, SwfMovie
from .place_object import PLACE_OBJECT_CODES, parse_place_object
from .planner import RescalePlan
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

    new_tags = parse_tags(new_body, first_tag_offset)
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
                tweens = plan.tweens.get(character_id)
                problems += _verify_stretched_sprite(movie.sprites[character_id], new, retiming,
                                                     tweens.steps if tweens else {})
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


def _verify_stretched_sprite(sprite: Sprite, new: Tag, retiming: Retiming, tweens) -> list[str]:
    """(a) the tags of original frame k (labels included) are unchanged, on new frame
       retiming.tick(k), and the frames in between contain only the planned interpolated moves
       (none, for objects that aren't tweened);
    (b) a display list simulation of the new timeline matches the original exactly on every original
       frame, and on the frames in between, tweened objects stay between their two keyframe states"""
    cid = sprite.character_id
    new_sprite_tags = parse_sprite_tags(new)
    old_frames, old_trailing = _split_frames(sprite.tags)
    new_frames, new_trailing = _split_frames(new_sprite_tags)
    if len(new_frames) != retiming.tick(len(old_frames)) or parse_sprite_header(new)[1] != len(new_frames):
        return [f"sprite {cid}: wrong frame count"]
    if new_trailing != old_trailing:
        return [f"sprite {cid}: tags after the last frame changed"]

    old_states, new_states = simulate_timeline(sprite.tags), simulate_timeline(new_sprite_tags)
    as_pairs = lambda tags: [(tag.code, tag.payload) for tag in tags]
    problems = []
    for k in range(len(old_frames)):
        start = retiming.tick(k)
        if as_pairs(new_frames[start]) != as_pairs(old_frames[k]):
            problems.append(f"sprite {cid} frame {k} tags changed")
        if new_states[start] != old_states[k]:
            problems.append(f"sprite {cid} frame {k} state differs from original")

        planned = {step.depth: step for step in tweens.get(k, [])}
        for tick in range(1, retiming.hold(k)):
            state = new_states[start + tick]
            tick_tags = new_frames[start + tick]
            if any(tag.code not in PLACE_OBJECT_CODES for tag in tick_tags) or \
                    {parse_place_object(tag).depth for tag in tick_tags} != set(planned):
                problems.append(f"sprite {cid} frame {k} tick {tick}: unexpected tags")
                continue
            for depth, step in planned.items():
                if (state[depth].instance, state[depth].display_options) != (step.start.instance, step.start.display_options):
                    problems.append(f"sprite {cid} f{k} tick {tick} depth {depth}: object or display options changed")
                for field in ("matrix", "color_transform"):
                    values = zip(getattr(state[depth], field), getattr(step.start, field), getattr(step.end, field))
                    for value, a, b in values:
                        if not min(a, b) - 1 <= value <= max(a, b) + 1:
                            problems.append(f"sprite {cid} f{k} tick {tick} {field} out of range")
            # Everything else on stage must be exactly as on the keyframe
            for depth, obj in state.items():
                if depth not in planned and obj != old_states[k].get(depth):
                    problems.append(f"sprite {cid} f{k} tick {tick} depth {depth} drifted")
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
