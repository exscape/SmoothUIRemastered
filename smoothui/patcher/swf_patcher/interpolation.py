"""Tween interpolation: find objects that move a little on every frame, and compute in-between states.

A timeline is simulated (display list per frame); runs of consecutive frames where the same object
changes its matrix / colour transform / morph ratio are taken to be tweens. When such a sprite is
stretched, the extra ticks between two of its keyframes can then carry interpolated moves rather
than just holding the old state. Jumps, swaps, appear/disappear etc. stay stepped.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from ...swf_tags import TagCode, parse_remove_object_depth
from .place_object import (
    FIXED_ONE,
    IDENTITY_COLOR_TRANSFORM,
    IDENTITY_MATRIX,
    PLACE_OBJECT_CODES,
    ColorTransform,
    Matrix,
    parse_place_object,
)

MAX_RATIO = 0xFFFF

# Changes between two frames smaller than this (see change_magnitude; roughly pixels) are not
# interpolated: the in-between states would differ by less than can be seen, so they would only add
# bytes. Such changes are counted as "subpixel" in TweenStats.
MIN_MOTION = 0.5

# A step more than this many times the median step of its run (and over OUTLIER_MIN_MOTION) is a
# jump, not part of the tween
OUTLIER_FACTOR = 8.0
OUTLIER_MIN_MOTION = 20
# Rotations larger than this per frame are ambiguous to interpolate (which way round?)
MAX_ROTATION_DEGREES = 60.0


@dataclass(frozen=True)
class DisplayState:
    """One object on the display list"""
    instance: int               # counts placements, so a re-placed or swapped object is a new instance
    character_id: int
    matrix: Matrix
    color_transform: ColorTransform
    ratio: int | None


Snapshot = dict  # depth -> DisplayState, after a ShowFrame


@dataclass(frozen=True)
class TweenStep:
    """An object that changes from `start` (frame k) to `end` (frame k + 1) and should be interpolated"""
    depth: int
    start: DisplayState
    end: DisplayState


@dataclass
class TweenStats:
    changes: int = 0        # object changes between consecutive frames
    interpolated: int = 0
    single_step: int = 0    # isolated changes: a jump, not a tween
    outlier: int = 0        # much larger than the rest of its run
    rotation: int = 0       # rotates too far to interpolate
    subpixel: int = 0       # too small to be worth interpolating

    @property
    def snapped(self) -> int:
        """Changes that stay stepped for a reason other than being too small to see"""
        return self.single_step + self.outlier + self.rotation


def simulate_timeline(tags) -> list[Snapshot]:
    """Simulate the display list of a timeline; returns its state at every ShowFrame"""
    current: Snapshot = {}
    snapshots = []
    placements = 0
    for tag in tags:
        if tag.code in PLACE_OBJECT_CODES:
            place = parse_place_object(tag)
            depth = place.depth
            if place.character_id is not None:
                # New object, or (when moving) a character swap; both count as a new instance
                placements += 1
                old = current.get(depth) if place.is_move else None
                current[depth] = DisplayState(
                    placements,
                    place.character_id,
                    place.matrix or (old.matrix if old else IDENTITY_MATRIX),
                    place.color_transform or (old.color_transform if old else IDENTITY_COLOR_TRANSFORM),
                    place.ratio if place.ratio is not None or not old else old.ratio,
                )
            elif place.is_move and depth in current:
                changes = {}
                if place.matrix:
                    changes["matrix"] = place.matrix
                if place.color_transform:
                    changes["color_transform"] = place.color_transform
                if place.ratio is not None:
                    changes["ratio"] = place.ratio
                current[depth] = dataclasses.replace(current[depth], **changes)
        elif tag.code in (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2):
            current.pop(parse_remove_object_depth(tag), None)
        elif tag.code == TagCode.SHOW_FRAME:
            snapshots.append(dict(current))
    return snapshots


def change_magnitude(a: DisplayState, b: DisplayState) -> float:
    """Size of the change a -> b in roughly pixel-sized units. Used to tell tweens from jumps,
    and to rank how much interpolating a sprite is worth."""
    dm = [abs(x - y) for x, y in zip(a.matrix, b.matrix)]
    translation = ((dm[4] / 20.0) ** 2 + (dm[5] / 20.0) ** 2) ** 0.5  # twips -> pixels
    magnitude = max(translation, max(dm[0], dm[1], dm[2], dm[3]) / FIXED_ONE * 100.0)

    dc = [abs(x - y) for x, y in zip(a.color_transform, b.color_transform)]
    magnitude = max(magnitude, max(dc[:4]) / 256.0 * 100.0, max(dc[4:]) / 255.0 * 50.0)

    if a.ratio is not None and b.ratio is not None:
        magnitude = max(magnitude, abs(a.ratio - b.ratio) / MAX_RATIO * 100.0)
    return magnitude


def _rotation(matrix: Matrix) -> float:
    return math.atan2(matrix.rotate_skew0 / FIXED_ONE, matrix.scale_x / FIXED_ONE)


def find_tweens(snapshots: list[Snapshot]) -> tuple[dict[int, list[TweenStep]], TweenStats]:
    """Find the frame transitions to interpolate.

    Returns ({k: [steps for the transition from frame k to k + 1]}, stats)."""
    stats = TweenStats()

    # (frame, depth) -> magnitude of the change from the previous frame
    changes = {}
    for k in range(1, len(snapshots)):
        for depth, b in snapshots[k].items():
            a = snapshots[k - 1].get(depth)
            if a is None or a.instance != b.instance:
                continue
            if (a.matrix, a.color_transform, a.ratio) != (b.matrix, b.color_transform, b.ratio):
                changes[(k, depth)] = change_magnitude(a, b)
    stats.changes = len(changes)

    tweens = {}
    for depth in sorted({depth for (_, depth) in changes}):
        frames = sorted(k for (k, d) in changes if d == depth)
        for run in _consecutive_runs(frames):
            if len(run) < 2:
                stats.single_step += 1
                continue
            magnitudes = [changes[(k, depth)] for k in run]
            for i, k in enumerate(run):
                others = sorted(magnitudes[:i] + magnitudes[i + 1:])
                median = others[len(others) // 2]
                if median > 0 and magnitudes[i] > OUTLIER_FACTOR * median and magnitudes[i] > OUTLIER_MIN_MOTION:
                    stats.outlier += 1
                    continue
                if magnitudes[i] < MIN_MOTION:
                    stats.subpixel += 1
                    continue
                a, b = snapshots[k - 1][depth], snapshots[k][depth]
                turn = math.degrees(_rotation(b.matrix) - _rotation(a.matrix) + math.pi) % 360 - 180
                if abs(turn) > MAX_ROTATION_DEGREES:
                    stats.rotation += 1
                    continue
                tweens.setdefault(k - 1, []).append(TweenStep(depth, a, b))
                stats.interpolated += 1
    return tweens, stats


def _consecutive_runs(sorted_values):
    runs, current = [], []
    for value in sorted_values:
        if current and value == current[-1] + 1:
            current.append(value)
        else:
            if current:
                runs.append(current)
            current = [value]
    if current:
        runs.append(current)
    return runs


def interpolate(a: DisplayState, b: DisplayState, t: float):
    """The state t (0..1) of the way from a to b, as (matrix, color transform, ratio).
    Properties that don't change are None, so they can be left out of the PlaceObject."""
    lerp = lambda x, y: round(x + (y - x) * t)
    matrix = Matrix(*map(lerp, a.matrix, b.matrix)) if a.matrix != b.matrix else None
    cxform = ColorTransform(*map(lerp, a.color_transform, b.color_transform)) \
        if a.color_transform != b.color_transform else None
    ratio = lerp(a.ratio, b.ratio) if a.ratio is not None and b.ratio is not None and a.ratio != b.ratio else None
    return matrix, cxform, ratio
