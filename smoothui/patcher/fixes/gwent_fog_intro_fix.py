"""gwint_game: the fog intro jumps partway through, on most casts.

CardFXFog_Ongoing's intro is a 50-frame clip (sprite 986) of large puffs drifting, which it fades in
at "start" and out a couple of seconds later. The clip is placed on the idle frame (0), invisible,
so it exists from the moment the board loads and keeps looping while the fog is idle. When the fog
is cast, it is at whatever frame the time since then happens to leave it at, and its loop's wrap
(the puffs jump back to where they started) usually comes while it is still visible.

The fix moves the clip's tags from the frames before "start" onto "start" itself, in order, so its
state there is unchanged. Going back to idle (frame 0) then removes the clip, and each cast creates
it afresh at its first frame, so it plays from the start, as it was meant to. The rain and frost
place their clips on or after their first played frame, so they already start in step.
"""

from __future__ import annotations

from ..swf_patcher.interpolation import simulate_timeline
from ..swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from ..swf_patcher.swf_tags import TagCode, parse_remove_object_depth
from .swf_edit import CantApply, FixKind, FixResult, SwfEdit, label_frames

NAME = "gwent-fog-intro-fix"
CONSEQUENCE = "in Gwent, the fog effect's intro usually jumps partway through"
KIND = FixKind.GAME_BUG
FOG_CLASS = "CardFXFog_Ongoing"
REMOVE_CODES = (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2)


def apply(swf: SwfEdit) -> FixResult:
    sprite_id, tags = swf.linked_sprite(FOG_CLASS)
    start = label_frames(tags).get("start")
    if not start:
        raise CantApply(f"{FOG_CLASS} has no \"start\" label after its first frame")

    # Clips that loop on their own while the fog is idle: animated, and invisible on the idle frame
    idle = simulate_timeline(tags)[0]
    depths = [depth for depth, state in idle.items()
              if len(simulate_timeline(swf.sprite_tags(state.character_id) or [])) > 1
              and state.color_transform.mul_a == 0]
    if not depths:
        return FixResult([], f"{FOG_CLASS} has no animated clip waiting invisibly on its idle frame")

    moved, kept, frame = [], [], 0
    for tag in tags:
        if frame < start and tag.code != TagCode.SHOW_FRAME and _depth(tag) in depths:
            moved.append(tag)
            continue
        kept.append(tag)
        if tag.code == TagCode.SHOW_FRAME:
            frame += 1
            if frame == start:
                kept += moved

    if any(tag.code in REMOVE_CODES for tag in moved):
        raise CantApply(f"{FOG_CLASS} removes its intro clip before \"start\"")
    swf.replace_sprite(sprite_id, kept)
    where = ", ".join(f"depth {depth}" for depth in depths)
    return FixResult([sprite_id], f"placed {FOG_CLASS}'s intro ({where}) on \"start\" (frame {start}) rather "
                                  "than its idle frame, so each cast plays it from its first frame")


def _depth(tag) -> int | None:
    if tag.code in PLACE_OBJECT_CODES:
        return parse_place_object(tag).depth
    if tag.code in REMOVE_CODES:
        return parse_remove_object_depth(tag)
    return None
