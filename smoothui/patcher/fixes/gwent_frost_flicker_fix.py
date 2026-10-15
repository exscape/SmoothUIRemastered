"""gwint_game: the frost row effect flickers slightly while it loops.

CardFXFrost_Ongoing loops on frames "loop" (50, 0-based) to 55, where a frame script jumps back to
"loop" before frame 55 is drawn. Those frames still change a few layers: two full-row overlays step
their alpha (0.238 -> 0.25, and 0.219 -> 0.23, the latter re-sending its ColorMatrix filter), and a
glow drifts by 0.15 px. The jump back undoes all of it, every 5 frames. The fog and rain loops have
no such changes.

The fix drops the moves after "loop", up to "ending", so the loop holds the state of its first
frame, which follows on smoothly from the fade-in. The fade-out works as before.
"""

from __future__ import annotations

from ..swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from ..swf_patcher.swf_tags import TagCode
from .swf_edit import CantApply, FixResult, SwfEdit, label_frames

NAME = "gwent-frost-flicker-fix"
FROST_CLASS = "CardFXFrost_Ongoing"
REMOVE_CODES = (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2)


def apply(swf: SwfEdit) -> FixResult:
    sprite_id, tags = swf.linked_sprite(FROST_CLASS)
    labels = label_frames(tags)
    if "loop" not in labels or "ending" not in labels or labels["loop"] >= labels["ending"]:
        raise CantApply(f"{FROST_CLASS} doesn't have a \"loop\" label followed by an \"ending\" label")
    frozen = range(labels["loop"] + 1, labels["ending"])

    kept, frame = [], 0
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            frame += 1
        if frame in frozen:
            if tag.code in REMOVE_CODES:
                raise CantApply(f"{FROST_CLASS}'s loop removes objects")
            if tag.code in PLACE_OBJECT_CODES:
                place = parse_place_object(tag)
                if not place.is_move or place.character_id is not None:
                    raise CantApply(f"{FROST_CLASS}'s loop places objects")
                continue
        kept.append(tag)

    dropped = len(tags) - len(kept)
    if not dropped:
        return FixResult([], f"{FROST_CLASS}'s loop is already static")
    swf.replace_sprite(sprite_id, kept)
    return FixResult([sprite_id], f"dropped {dropped} moves from {FROST_CLASS}'s loop "
                                  f"(frames {frozen.start}-{frozen.stop - 1}), so it holds its first frame")
