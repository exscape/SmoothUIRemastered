"""Building the new SWF body from the original and a plan"""

from __future__ import annotations

import struct
from collections import defaultdict

from ..swf import FPS_FIXED_POINT_SCALE, MAX_RAW_FPS
from .abc_bytecode import assemble
from .abc_file import serialize_abc
from .movie import RescaleError, Sprite, SwfMovie
from .planner import RescalePlan
from .retiming import Retiming
from .swf_tags import Tag, TagCode, build_sprite, serialize_tags

SHOW_FRAME = Tag(TagCode.SHOW_FRAME, b"")


def raw_fps(fps: float) -> int:
    raw = round(fps * FPS_FIXED_POINT_SCALE)
    if not 1 <= raw <= MAX_RAW_FPS:
        raise RescaleError(f"fps {fps:.2f} out of range (max 255.99)")
    return raw


def stretch_sprite(sprite: Sprite, tag: Tag, retiming: Retiming, moves: dict[int, list[Tag]]) -> Tag:
    """Hold every frame of the sprite for retiming.hold(frame) frames. The interpolated moves of the
    tweened objects (see interpolation.tween_moves) go on the frames they are planned for: after the
    original tags, on a keyframe's own frame. The extra frames are otherwise empty."""
    tags, frame = [], 0
    for sub in sprite.tags:
        if sub.code != TagCode.SHOW_FRAME:
            tags.append(sub)
            continue
        start = retiming.tick(frame)
        for tick in range(start, start + retiming.hold(frame)):
            tags += moves.get(tick, [])
            tags.append(sub if tick == start else SHOW_FRAME)
        frame += 1
    return build_sprite(sprite.character_id, retiming.tick(frame), tags, long_header=tag.long_header)


def build_body(movie: SwfMovie, plan: RescalePlan) -> bytes:
    tags = list(movie.tags)

    for character_id, retiming in plan.sprite_retiming.items():
        sprite = movie.sprites[character_id]
        tweens = plan.tweens.get(character_id)
        tags[sprite.tag_index] = stretch_sprite(sprite, tags[sprite.tag_index], retiming,
                                                tweens.moves if tweens else {})

    new_bodies = defaultdict(dict)  # DoABC tag index -> {method index in its ABC: new body}
    for method, edits in plan.code_edits.items():
        unit = movie.unit_of(method)
        body = unit.abc.bodies[unit.local(method)]
        new_bodies[unit.tag_index][unit.local(method)] = assemble(body.code, movie.code[method], edits, body.exceptions)
    for unit in movie.units:
        if unit.tag_index in new_bodies:
            abc_bytes = serialize_abc(unit.abc, new_bodies[unit.tag_index], plan.new_doubles.get(unit.tag_index, ()))
            old = tags[unit.tag_index]
            tags[unit.tag_index] = Tag(TagCode.DO_ABC, unit.prefix + abc_bytes, old.long_header)

    header = bytearray(movie.body[:movie.first_tag_offset])
    struct.pack_into("<H", header, movie.fps_offset, raw_fps(plan.target_fps))
    return bytes(header) + serialize_tags(tags)
