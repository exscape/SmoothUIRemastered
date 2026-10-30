"""Telling a mod's file that only raised the frame rate in its header from one that retimed its animations.

Some mods make a screen smoother by raising its SWF header's frame rate, and nothing else. Its timeline
animations then play that much faster: they were made for the game's rate. Rescaled from the header's rate,
they would stay too fast, so such a file is rescaled from the game's rate instead, as if the mod hadn't
raised it. Only where the file shows it: every clip the rescale stretches must be one of the game's clips,
with the same frame count, and no per-frame code may be scaled (code can't be compared with the game's).
Anything else, such as a mod that retimed its clips for the new rate (which changes their frame counts), or
added an animation the game doesn't have, keeps the header's rate: it then plays as it does with that mod
alone.

A clip is one of the game's if it has the same frame count as a game clip of the same name (see clip_name),
in any of the game's files (mods copy clips from other screens), or the same timing pattern as a game clip
(for clips without a name, the only way to tell): its frame count, labels, and for each frame which depths are placed, moved (and which
properties a move sets) or removed. The pattern leaves out what a change sets, and character ids (Flash
renumbers them, and a mod's new art changes them).

What the game's files hold comes from DATA_FILE, which build-main.py writes from the uncooked game files (see
build/game_timings.py), so the merger needs no game files for it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ..common import SmoothUIError, profile_key
from .swf_patcher.place_object import PLACE_OBJECT_CODES, parse_place_object
from .swf_patcher.swf_tags import TagCode, parse_frame_label, parse_remove_object_depth

DATA_FILE = Path(__file__).with_name("game_timings.txt")

# The first line of DATA_FILE. A change to what it holds or how (timing_pattern, clip_name) must change it,
# so a stale file can't be used.
FORMAT_LINE = "smoothui game timings 2"

REMOVE_CODES = (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2)

# The class names Flash makes up for library clips: the file's name, "_fla.", the symbol's name, and a number
# that changes whenever the file is saved (panel_alchemy_fla.InputFeedbackButton_icon_steam_42)
FLA_CLASS = re.compile(r"\w+_fla\.(.+?)(?:_\d+)?")


def clip_name(class_name: str) -> str:
    """A clip's name in any file: its class name, less what Flash adds to it (see FLA_CLASS)"""
    match = FLA_CLASS.fullmatch(class_name)
    return match.group(1) if match else class_name


def timing_pattern(tags) -> str:
    """The timing pattern of a clip (see above), from its timeline tags, as 16 hex digits"""
    frames, changes, labels = [], [], []
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            frames.append(tuple(sorted(changes)))
            changes = []
        elif tag.code == TagCode.FRAME_LABEL:
            labels.append((len(frames), parse_frame_label(tag)))
        elif tag.code in PLACE_OBJECT_CODES:
            place = parse_place_object(tag)
            changes.append((place.depth, "move" if place.is_move else "place", place.character_id is not None,
                            place.matrix is not None, place.color_transform is not None, place.ratio is not None))
        elif tag.code in REMOVE_CODES:
            changes.append((parse_remove_object_depth(tag), "remove", False, False, False, False))
    return hashlib.sha256(repr((labels, frames)).encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class GameTimings:
    """What DATA_FILE holds; clips are those with more than one frame"""
    fps: dict[str, float]               # profile key -> the game file's header frame rate
    frames: dict[str, frozenset]        # clip name (see clip_name) -> the frame counts of the game's clips
    patterns: frozenset                 # the timing patterns of the game's clips


@cache
def game_timings() -> GameTimings:
    try:
        lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise SmoothUIError(f"can't read {DATA_FILE.name} ({e}); build-main.py writes it") from None
    if not lines or lines[0] != FORMAT_LINE:
        raise SmoothUIError(f"{DATA_FILE.name} is from another version of the patcher; build-main.py writes it")
    fps, frames, patterns = {}, {}, set()
    for line in lines[1:]:
        kind, _, rest = line.partition(" ")
        if kind == "fps":
            path, _, value = rest.rpartition(" ")
            fps[profile_key(path)] = float(value)
        elif kind == "frames":
            name, _, counts = rest.rpartition(" ")
            frames[name] = frozenset(map(int, counts.split(",")))
        elif kind == "timing":
            patterns.add(rest)
    return GameTimings(fps, frames, frozenset(patterns))


def game_fps(path) -> float | None:
    """The header frame rate of the game's own copy of a file; None if the game has no such file"""
    return game_timings().fps.get(profile_key(path))


def is_game_clip(movie, character_id) -> bool:
    """Whether a clip is one of the game's clips, with the same frame count (see above)"""
    timings = game_timings()
    sprite = movie.sprites[character_id]
    class_name = movie.class_name(character_id)
    if class_name and sprite.frame_count in timings.frames.get(clip_name(class_name), ()):
        return True
    return timing_pattern(sprite.tags) in timings.patterns


def not_game_timed(movie, plan) -> str | None:
    """Why a rescale plan made from the game's frame rate can't be trusted to be right (see above): a
    stretched clip that isn't one of the game's, or scaled per-frame code; None if it can"""
    new = sorted(character_id for character_id in plan.stretched if not is_game_clip(movie, character_id))
    if new:
        shown = ", ".join(map(str, new[:5])) + (", ..." if len(new) > 5 else "")
        return f"animations the game doesn't have (sprite{'s' if len(new) > 1 else ''} {shown})"
    if plan.increments:
        return "code that animates on every frame"
    return None
