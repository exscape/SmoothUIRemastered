"""gwint_game: the coin flip's shadow can't follow the coin at a high frame rate.

COINA_113 and COINB_116 (the two coins of the toss at the start of a game, which have the same layout)
have two layers each. Depth 2 is the coin, which moves a
little on every frame, so it is interpolated. Depth 1 is its dark shadow: a different traced outline
(a shape of its own) on each of 16 frames, which can't be interpolated. At a high frame rate the
shadow then steps at the original 24 fps while the coin moves smoothly, and the two seem to tear
apart.

The fix removes the shadow's tags, so the coins have no shadow.
"""

from __future__ import annotations

from ..swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from ..swf_patcher.swf_tags import TagCode, parse_remove_object_depth
from .swf_edit import CantApply, FixResult, SwfEdit

NAME = "gwent-coin-shadow-fix"
COIN_CLASSES = ("gwint_game_fla.COINA_113", "gwint_game_fla.COINB_116")
SHADOW_DEPTH = 1
REMOVE_CODES = (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2)


def apply(swf: SwfEdit) -> FixResult:
    sprites, details = [], []
    for class_name in COIN_CLASSES:
        sprite_id, tags = swf.linked_sprite(class_name)
        shadow = [tag for tag in tags
                  if (tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).depth == SHADOW_DEPTH)
                  or (tag.code in REMOVE_CODES and parse_remove_object_depth(tag) == SHADOW_DEPTH)]
        if not shadow:
            continue
        if sum(tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).character_id is not None
               for tag in shadow) < 2:
            raise CantApply(f"depth {SHADOW_DEPTH} of {class_name} isn't a changing shape")
        swf.replace_sprite(sprite_id, [tag for tag in tags if not any(tag is other for other in shadow)])
        sprites.append(sprite_id)
        details.append(f"{class_name} ({len(shadow)} tags)")
    if not sprites:
        return FixResult([], f"the coins have nothing on depth {SHADOW_DEPTH}")
    return FixResult(sprites, f"removed the shadow (depth {SHADOW_DEPTH}) from {', '.join(details)}")
