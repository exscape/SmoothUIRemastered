"""gwint_game: the falling rain stops for a frame on every loop.

CardFXRain_Ongoing's rain sheets (sprites without a class, placed directly in it) each loop by moving
two copies of one image along, one image apart. Their last frame shows the same picture as their
first: the trailing copy is exactly where the leading one started. As the timeline then wraps to the
first frame, that picture shows twice in a row, and the rain stops for a frame on every loop: up to
four times a second, for the shortest sheet.

The fix drops that last frame. It runs after the rescale, so that the frames the rescale adds before
it still move the rain towards it, and the loop continues smoothly into the first frame. Before the
rescale, those frames would hold still instead: the wrap itself is a jump (each copy moves back by
one image), which isn't interpolated.
"""

from __future__ import annotations

from ..swf_patcher.interpolation import DisplayState, simulate_timeline
from ..swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from ..swf_patcher.swf_tags import TagCode
from .swf_edit import AFTER_RESCALE, CantApply, FixKind, FixResult, SwfEdit

NAME = "gwent-rain-loop-fix"
CONSEQUENCE = "in Gwent, the falling rain stops for a frame on every loop"
KIND = FixKind.GAME_BUG
PHASE = AFTER_RESCALE
RAIN_CLASS = "CardFXRain_Ongoing"


def apply(swf: SwfEdit) -> FixResult:
    _, rain_tags = swf.linked_sprite(RAIN_CLASS)
    children = sorted({place.character_id for place in map(parse_place_object, _place_tags(rain_tags))
                       if place.character_id is not None})
    sheets = {}     # character id -> (timeline, display list snapshots)
    for character_id in children:
        tags = swf.sprite_tags(character_id)
        if tags is None or swf.class_of(character_id) is not None:
            continue
        snapshots = simulate_timeline(tags)
        if _is_sheet(snapshots):
            sheets[character_id] = tags, snapshots
    if not sheets:
        raise CantApply(f"found no rain sheets (two copies of one image, moving along) in {RAIN_CLASS}")

    fixed = []
    for character_id, (tags, snapshots) in sheets.items():
        if not _last_frame_repeats_first(snapshots):
            continue
        # The last frame, and the frames after it that only hold it (when rescaled)
        start = len(snapshots) - 1
        while start > 0 and snapshots[start - 1] == snapshots[-1]:
            start -= 1
        frames, trailing = _split_frames(tags)
        if any(tag.code == TagCode.FRAME_LABEL for frame in frames[start:] for tag in frame):
            raise CantApply(f"rain sheet {character_id}'s last frame has a label")
        swf.replace_sprite(character_id, [tag for frame in frames[:start] for tag in frame] + trailing,
                           frames_removed=len(frames) - start)
        fixed.append(character_id)

    if not fixed:
        return FixResult([], f"no rain sheet in {RAIN_CLASS} repeats its first frame at the end")
    return FixResult(fixed, f"dropped the last frame of rain sheets {', '.join(map(str, fixed))}, "
                            "which repeats their first")


def _place_tags(tags):
    return [tag for tag in tags if tag.code in PLACE_OBJECT_CODES]


def _is_sheet(snapshots: list[dict]) -> bool:
    """Two copies of one character, at the same two depths throughout, moving along"""
    if len(snapshots) < 3 or len(snapshots[0]) != 2:
        return False
    depths = snapshots[0].keys()
    if any(snapshot.keys() != depths for snapshot in snapshots):
        return False
    return len({state.character_id for snapshot in snapshots for state in snapshot.values()}) == 1


def _last_frame_repeats_first(snapshots: list[dict]) -> bool:
    """On the last frame, one copy is exactly where the other one was on the first frame"""
    first, last = snapshots[0], snapshots[-1]
    a, b = sorted(first)
    return _looks(last[a]) == _looks(first[b]) or _looks(last[b]) == _looks(first[a])


def _looks(state: DisplayState) -> tuple:
    """What a display list object looks like, regardless of which placement it is"""
    return state.character_id, state.matrix, state.color_transform, state.ratio, state.display_options


def _split_frames(tags) -> tuple[list[list], list]:
    """A timeline's frames (each a list of tags, ending with its ShowFrame), and the tags after the
    last ShowFrame (the End tag)"""
    frames, current = [], []
    for tag in tags:
        current.append(tag)
        if tag.code == TagCode.SHOW_FRAME:
            frames.append(current)
            current = []
    return frames, current
