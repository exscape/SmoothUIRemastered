"""Freeze animations that aren't required, and would otherwise animate too quickly"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..build_common import fail
from ..swf import (
    SPRITE_FIELDS_SIZE,
    swf_get_header_offsets,
)
from ..swf_tags import (
    END_TAG,
    SHOW_FRAME_TAG,
    Tag,
    TagCode,
    encode_tag_header,
    iter_tags,
)

# PlaceObject2 payload: a flags byte, then Depth (UI16), then optional fields
PLACE_OBJECT2_MIN_LENGTH = 3
PLACE_FLAG_HAS_CHARACTER = 0x02
PLACE_FLAG_HAS_COLOR_TRANSFORM = 0x08


@dataclass(frozen=True)
class FocusAnimation:
    """A DefineSprite that should be reduced to its first frame."""
    tag: Tag
    sprite_id: int
    frame_count: int
    tween_count: int
    first_frame_end: int    # offset just past the sprite's first ShowFrame tag




#
#
# TODO: tweak values prior to commit: tweens == frames (or possibly tweens == frames - 1, if the first frame doesn't count despite it's alpha transform)
# TODO: is stricter and most likely still matches 100%
#
#
# Focus animation patching constants
#
# A sprite could be is a focus animation if it has at least this many frames...
MIN_ANIMATED_FRAMES = 20
# ...and at least this many PlaceObject2 tags that change the color transform of an object
# already on the stage (this is how the alpha fades are encoded).
MIN_COLOR_TWEENS = 6

def is_color_transform_update(body: bytearray, tag: Tag) -> bool:
    """True for a PlaceObject2 that only modifies the color transform of an existing object."""
    if tag.length < PLACE_OBJECT2_MIN_LENGTH:
        fail(f"PlaceObject2 at offset {tag.start} is too short ({tag.length} bytes)")
    flags = body[tag.payload_start]
    relevant = PLACE_FLAG_HAS_CHARACTER | PLACE_FLAG_HAS_COLOR_TRANSFORM
    return (flags & relevant) == PLACE_FLAG_HAS_COLOR_TRANSFORM

def inspect_sprite(body: bytearray, tag: Tag) -> FocusAnimation | None:
    """Return a FocusAnimation if this DefineSprite should be frozen, otherwise None."""
    if tag.length < SPRITE_FIELDS_SIZE:
        fail(f"DefineSprite at offset {tag.start} is too short ({tag.length} bytes)")
    sprite_id, frame_count = struct.unpack_from("<HH", body, tag.payload_start)
    if frame_count < MIN_ANIMATED_FRAMES:
        return None

    tween_count = 0
    first_frame_end = None
    for inner in iter_tags(body, tag.payload_start + SPRITE_FIELDS_SIZE, tag.end):
        if inner.code == TagCode.FRAME_LABEL:
            # Labelled frames are jump targets for scripts (gotoAndPlay etc.), so removing
            # frames could break them. Leave such sprites alone.
            return None
        if inner.code == TagCode.PLACE_OBJECT2:
            if is_color_transform_update(body, inner):
                tween_count += 1
        elif inner.code == TagCode.SHOW_FRAME and first_frame_end is None:
            first_frame_end = inner.end

    if tween_count < MIN_COLOR_TWEENS:
        return None
    if first_frame_end is None:
        fail(f"DefineSprite {sprite_id} at offset {tag.start} has no ShowFrame tag")
    return FocusAnimation(tag, sprite_id, frame_count, tween_count, first_frame_end)

def find_focus_animations(body: bytearray) -> list[FocusAnimation]:
    """Scan the whole tag stream, validating it, without modifying anything."""
    _, first_tag_offset = swf_get_header_offsets(body)
    if first_tag_offset is None:
        fail("not a valid SWF body: cannot locate the tag stream")

    found = []
    for tag in iter_tags(body, first_tag_offset, len(body)):
        if tag.code == TagCode.DEFINE_SPRITE:
            animation = inspect_sprite(body, tag)
            if animation is not None:
                found.append(animation)
    return found

def freeze_sprite(body: bytearray, animation: FocusAnimation) -> None:
    """Rebuild the sprite as its first frame followed by empty frames.

    The frame count is preserved, so the sprite still reports and plays the same number
    of frames, but nothing changes after the first one."""
    tag = animation.tag
    first_frame = bytes(body[tag.payload_start:animation.first_frame_end])  # id, frame count, frame 1
    payload = first_frame + SHOW_FRAME_TAG * (animation.frame_count - 1) + END_TAG
    header = encode_tag_header(TagCode.DEFINE_SPRITE, len(payload), force_long=True)
    body[tag.start:tag.end] = header + payload

def freeze_focus_animations(body: bytearray) -> int:
    """Freeze every focus animation in the SWF body, in place. Returns how many were frozen.

    A focus animation is a DefineSprite with at least MIN_ANIMATED_FRAMES frames and at
    least MIN_COLOR_TWEENS color-transform updates (alpha fades), and no FrameLabel.
    Everything after its first frame is removed, which shrinks the body.

    The tag stream is fully validated before anything is modified; fails if it is malformed."""
    animations = find_focus_animations(body)
    for a in animations:
        print(f"[*] Frozen focus animation -> Sprite ID: {a.sprite_id} "
              f"(Frames: {a.frame_count}, Tweens: {a.tween_count})")

    # Rewrite back to front, so the offsets of the sprites still to be rewritten stay valid
    for a in reversed(animations):
        freeze_sprite(body, a)
    return len(animations)