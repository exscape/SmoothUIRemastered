"""hud_wolfstatbars: removes the brighter outline around Geralt's health bar. Optional: a matter of taste, so no
profile names it, and the merger applies it if the user chose to (see requires.OPTIONAL_FIXES).

The outline is mcGainGlow, a solid red layer behind the bar's fill, in BackgroundHealthIndicator. The clip has a
fade from bright to dark red, but it stops on its first frame and no code plays it.

The fix removes mcGainGlow's tags. No code refers to it, and the rest of the bar is left as it was. A copy without
the clip has nothing to fix: a mod's copy mustn't be reported as lacking a fix the user only chose as a look.
"""

from __future__ import annotations

from ..swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from ..swf_patcher.swf_tags import TagCode, parse_remove_object_depth
from .swf_edit import Fix, FixKind, FixResult, SwfEdit

BAR_CLASS = "BackgroundHealthIndicator"
GLOW_NAME = "mcGainGlow"
REMOVE_CODES = (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2)


def apply(swf: SwfEdit) -> FixResult:
    sprite_ids = swf.sprites_of_class(BAR_CLASS)
    if len(sprite_ids) != 1:
        return FixResult([], f"expected 1 sprite linked to {BAR_CLASS}, found {len(sprite_ids)}")
    sprite_id, = sprite_ids
    tags = swf.sprite_tags(sprite_id)
    assert tags is not None     # an id from sprites_of_class()
    depths = {parse_place_object(tag).depth for tag in tags
              if tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).name == GLOW_NAME}
    if not depths:
        return FixResult([], f"{BAR_CLASS} has no {GLOW_NAME}")
    glow = [tag for tag in tags
            if (tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).depth in depths)
            or (tag.code in REMOVE_CODES and parse_remove_object_depth(tag) in depths)]
    swf.replace_sprite(sprite_id, [tag for tag in tags if not any(tag is other for other in glow)])
    return FixResult([sprite_id], f"removed {GLOW_NAME} from {BAR_CLASS} ({len(glow)} tags)")


FIX = Fix(name="health-bar-glow-fix",
          kind=FixKind.SMOOTHUI_FEATURE,
          consequence="the brighter outline around the health bar stays",
          apply=apply)
