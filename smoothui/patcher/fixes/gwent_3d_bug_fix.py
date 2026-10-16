"""gwint_game: fog (and the fog layer inside rain) draws far outside its row, and flickers.

Fog_loop_bg_70, the fog layer of CardFXFog_Ongoing and CardFXRain_Ongoing, has a fully transparent
child named __id0_, which its class drives with an AnimatorFactory3D: it gets a matrix3D, so
Scaleform renders it as a 3D object (GwintGameMenu sets a perspective projection on the root). Inside
a mask, that makes Scaleform stop applying the mask. It's the only 3D animator in the file.

The fix removes the child. Nothing changes visually: it is transparent, and the animator keeps it
so. The animator finds no target and does nothing.
"""

from __future__ import annotations

from ..swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from .swf_edit import CantApply, FixResult, SwfEdit

NAME = "gwent-3d-bug-fix"
FOG_BG_CLASS = r"gwint_game_fla\.Fog_loop_bg_\d+"
CHILD_NAME = "__id0_"


def apply(swf: SwfEdit) -> FixResult:
    sprite_id, class_name, tags = swf.linked_sprite_matching(FOG_BG_CLASS)
    placements = [tag for tag in tags if tag.code in PLACE_OBJECT_CODES and parse_place_object(tag).name == CHILD_NAME]
    if not placements:
        return FixResult([], f"{class_name} has no {CHILD_NAME} child")
    for tag in placements:
        place = parse_place_object(tag)
        if place.color_transform is None or place.color_transform.mul_a != 0:
            raise CantApply(f"{class_name}'s {CHILD_NAME} is not transparent, so removing it would change the look")
    swf.replace_sprite(sprite_id, [tag for tag in tags if not any(tag is placement for placement in placements)])
    return FixResult([sprite_id], f"removed the 3D animator target {CHILD_NAME} from {class_name}")
