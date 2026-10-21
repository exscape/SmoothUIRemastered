"""Tween interpolation: find objects that move a little on every frame, and compute in-between states.

A timeline is simulated (display list per frame); runs of consecutive frames where the same object
changes its matrix / colour transform / morph ratio are taken to be tweens. When such a sprite is
stretched, the extra ticks between two of its keyframes can then carry interpolated moves rather
than just holding the old state. Jumps, swaps, appear/disappear etc. stay stepped. Over a run of
tween steps, objects move at an even pace (see sample_tweens), so keyframes whose holds differ in
length may be shown slightly before or after their exact states.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from typing import NamedTuple

from .place_object import (
    FIXED_ONE,
    IDENTITY_COLOR_TRANSFORM,
    IDENTITY_MATRIX,
    PLACE_OBJECT_CODES,
    ColorTransform,
    Matrix,
    build_move,
    parse_place_object,
)
from .retiming import Retiming
from .swf_tags import Tag, TagCode, parse_remove_object_depth

MAX_RATIO = 0xFFFF

# Changes between two frames smaller than this (see change_magnitude; roughly pixels) are not
# interpolated: the in-between states would differ by less than can be seen, so they would only add
# bytes. Such changes are counted as "subpixel" in TweenStats.
MIN_MOTION = 0.5

# A step is a jump, not part of the tween, if it is over OUTLIER_MIN_MOTION and more than
# OUTLIER_FACTOR times the median step of its run AND more than OUTLIER_NEIGHBOUR_FACTOR times the
# larger of its neighbours in the run. The median alone misfires on a run that drifts slowly and then
# moves fast (an eased move, a fade over a few frames): its median is tiny, while each of its fast
# steps is close to the ones around it. A step that stands out from its neighbours (an object popping
# in or out in one frame) stays a jump: whether that was meant to be instant can't be told.
OUTLIER_FACTOR = 8.0
OUTLIER_NEIGHBOUR_FACTOR = 3.0
OUTLIER_MIN_MOTION = 20
# Rotations larger than this per frame are ambiguous to interpolate (which way round?)
MAX_ROTATION_DEGREES = 60.0
# The wrap from a sprite's last frame to its first is interpolated if the first frame is within this
# fraction of the step's size of where the last tween step, repeated, would end up (see _continues)
LOOP_TOLERANCE = 0.5


class DisplayState(NamedTuple):
    """One object on the display list (a NamedTuple, as the timeline simulations make a great many)"""
    instance: int               # counts placements, so a re-placed or swapped object is a new instance
    character_id: int
    matrix: Matrix
    color_transform: ColorTransform
    ratio: int | None
    display_options: tuple = ()     # see PlaceObject; each option stays set until a tag changes it


Snapshot = dict  # depth -> DisplayState, after a ShowFrame


@dataclass(frozen=True, slots=True)
class TweenStep:
    """An object that changes from `start` (frame k) to `end` (frame k + 1) and should be interpolated"""
    depth: int
    start: DisplayState
    end: DisplayState


@dataclass(slots=True)
class TweenStats:
    changes: int = 0        # object changes between consecutive frames
    interpolated: int = 0
    single_step: int = 0    # isolated changes: a jump, not a tween
    outlier: int = 0        # much larger than the steps around it
    rotation: int = 0       # rotates too far to interpolate
    subpixel: int = 0       # too small to be worth interpolating

    @property
    def snapped(self) -> int:
        """Changes that stay stepped for a reason other than being too small to see"""
        return self.single_step + self.outlier + self.rotation


@dataclass(frozen=True, slots=True)
class TweenSample:
    """A tweened object on a new frame (tick) between keyframes: t (0..1) of the way through step"""
    step: TweenStep
    t: Fraction

    @property
    def depth(self) -> int:
        return self.step.depth


class DisplayList:
    """A timeline's display list, as its tags are applied one by one"""

    def __init__(self):
        self.objects: Snapshot = {}
        self.placements = 0

    def apply(self, tag):
        current = self.objects
        if tag.code in PLACE_OBJECT_CODES:
            place = parse_place_object(tag)
            depth = place.depth
            if place.character_id is not None:
                # New object, or (when moving) a character swap; both count as a new instance
                self.placements += 1
                old = current.get(depth) if place.is_move else None
                current[depth] = DisplayState(
                    self.placements,
                    place.character_id,
                    place.matrix or (old.matrix if old else IDENTITY_MATRIX),
                    place.color_transform or (old.color_transform if old else IDENTITY_COLOR_TRANSFORM),
                    place.ratio if place.ratio is not None or not old else old.ratio,
                    _merge_options(old.display_options if old else (), place.display_options),
                )
            elif place.is_move and depth in current:
                changes = {"display_options": _merge_options(current[depth].display_options, place.display_options)}
                if place.matrix:
                    changes["matrix"] = place.matrix
                if place.color_transform:
                    changes["color_transform"] = place.color_transform
                if place.ratio is not None:
                    changes["ratio"] = place.ratio
                current[depth] = current[depth]._replace(**changes)
        elif tag.code in (TagCode.REMOVE_OBJECT, TagCode.REMOVE_OBJECT2):
            current.pop(parse_remove_object_depth(tag), None)


def simulate_timeline(tags) -> list[Snapshot]:
    """Simulate the display list of a timeline; returns its state at every ShowFrame"""
    display, snapshots = DisplayList(), []
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            snapshots.append(dict(display.objects))
        else:
            display.apply(tag)
    return snapshots


def _merge_options(old: tuple, new: tuple) -> tuple:
    if not new:
        return old      # already merged (sorted, one entry per option)
    return tuple(sorted({**dict(old), **dict(new)}.items()))


def change_magnitude(a: DisplayState, b: DisplayState) -> float:
    """Size of the change a -> b in roughly pixel-sized units. Used to tell tweens from jumps,
    and to rank how much interpolating a sprite is worth."""
    am, bm = a.matrix, b.matrix
    translation = ((abs(am[4] - bm[4]) / 20.0) ** 2 + (abs(am[5] - bm[5]) / 20.0) ** 2) ** 0.5  # twips -> pixels
    magnitude = max(translation, max(abs(am[0] - bm[0]), abs(am[1] - bm[1]), abs(am[2] - bm[2]),
                                     abs(am[3] - bm[3])) / FIXED_ONE * 100.0)

    ac, bc = a.color_transform, b.color_transform
    if ac != bc:
        magnitude = max(magnitude,
                        max(abs(ac[0] - bc[0]), abs(ac[1] - bc[1]), abs(ac[2] - bc[2]), abs(ac[3] - bc[3])) / 256.0 * 100.0,
                        max(abs(ac[4] - bc[4]), abs(ac[5] - bc[5]), abs(ac[6] - bc[6]), abs(ac[7] - bc[7])) / 255.0 * 50.0)

    if a.ratio is not None and b.ratio is not None:
        magnitude = max(magnitude, abs(a.ratio - b.ratio) / MAX_RATIO * 100.0)
    return magnitude


def _rotation(matrix: Matrix) -> float:
    return math.atan2(matrix.rotate_skew0 / FIXED_ONE, matrix.scale_x / FIXED_ONE)


def find_tweens(snapshots: list[Snapshot]) -> tuple[dict[int, list[TweenStep]], TweenStats]:
    """Find the frame transitions to interpolate.

    Returns ({k: [steps for the transition from frame k to k + 1]}, stats); for the last frame,
    that is the wrap back to frame 0."""
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
                neighbour = max(magnitudes[max(i - 1, 0):i] + magnitudes[i + 1:i + 2])
                if (median > 0 and magnitudes[i] > OUTLIER_FACTOR * median
                        and magnitudes[i] > OUTLIER_NEIGHBOUR_FACTOR * neighbour
                        and magnitudes[i] > OUTLIER_MIN_MOTION):
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

    # The wrap from the last frame back to the first, when a looping tween continues across it. Left
    # stepped, it holds one object still for a frame while the rest keep moving; nested rotations that
    # nearly cancel out (Gwent's fog) then visibly lurch on every wrap.
    last = len(snapshots) - 1
    for step in tweens.get(last - 1, []) if last >= 2 else []:
        a, b = step.end, snapshots[0].get(step.depth)
        if b is None or b.instance != a.instance or b == a or not _continues(step.start, a, b):
            continue
        stats.changes += 1
        turn = math.degrees(_rotation(b.matrix) - _rotation(a.matrix) + math.pi) % 360 - 180
        if change_magnitude(a, b) < MIN_MOTION:
            stats.subpixel += 1
        elif abs(turn) > MAX_ROTATION_DEGREES:
            stats.rotation += 1
        else:
            tweens.setdefault(last, []).append(TweenStep(step.depth, a, b))
            stats.interpolated += 1
    return tweens, stats


def _continues(before: DisplayState, a: DisplayState, b: DisplayState) -> bool:
    """Whether a -> b continues the motion before -> a: b is about where one more such step would
    take a. A loop that resets instead (the end of a one-shot animation) is not interpolated."""
    extrapolate = lambda x, y: [2 * y_ - x_ for x_, y_ in zip(x, y)]
    ratio = 2 * a.ratio - before.ratio if a.ratio is not None and before.ratio is not None else a.ratio
    predicted = a._replace(matrix=Matrix(*extrapolate(before.matrix, a.matrix)),
                           color_transform=ColorTransform(*extrapolate(before.color_transform, a.color_transform)),
                           ratio=ratio)
    return change_magnitude(predicted, b) <= LOOP_TOLERANCE * change_magnitude(a, b)


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


def interpolate(a: DisplayState, b: DisplayState, t: float) -> DisplayState:
    """The state t (0..1) of the way from a to b: a, with b's matrix, colour transform and morph ratio
    blended in"""
    if t == 0:
        return a
    lerp = lambda x, y: round(x + (y - x) * t)
    ratio = lerp(a.ratio, b.ratio) if a.ratio is not None and b.ratio is not None else a.ratio
    return a._replace(matrix=_interpolate_matrix(a.matrix, b.matrix, t),
                      color_transform=ColorTransform(*map(lerp, a.color_transform, b.color_transform)),
                      ratio=ratio)


def _interpolate_matrix(a: Matrix, b: Matrix, t: float) -> Matrix:
    """Turns and scales each axis (the images of the x and y axes) along an arc, rather than moving
    it in a straight line: blending two rotations linearly shrinks the object in between (by 0.2%
    at 7.5 degrees per frame), which makes rotating objects pulse at the keyframe rate. Large turns,
    such as flips, are blended linearly, as Flash does."""
    if a == b:
        return a
    lerp = lambda x, y: round(x + (y - x) * t)
    axes = []
    for (ax, ay), (bx, by) in (((a.scale_x, a.rotate_skew0), (b.scale_x, b.rotate_skew0)),
                               ((a.rotate_skew1, a.scale_y), (b.rotate_skew1, b.scale_y))):
        start, turn = math.atan2(ay, ax), math.atan2(by, bx) - math.atan2(ay, ax)
        turn = (turn + math.pi) % (2 * math.pi) - math.pi
        if (ax, ay) == (bx, by) or not (ax or ay) or not (bx or by) or abs(math.degrees(turn)) > MAX_ROTATION_DEGREES:
            axes.append((lerp(ax, bx), lerp(ay, by)))
            continue
        length = math.hypot(ax, ay) + (math.hypot(bx, by) - math.hypot(ax, ay)) * t
        angle = start + turn * t
        axes.append((round(length * math.cos(angle)), round(length * math.sin(angle))))
    (scale_x, skew0), (skew1, scale_y) = axes
    return Matrix(scale_x, scale_y, skew0, skew1, lerp(a.translate_x, b.translate_x), lerp(a.translate_y, b.translate_y))


def sample_tweens(steps: dict[int, list[TweenStep]], retiming: Retiming,
                  anchors: set[int]) -> dict[int, list[TweenSample]]:
    """Where each tweened object is on each new frame (tick) between keyframes, as {tick: samples}.

    When the frames are held for different numbers of ticks (2, 3, 2, 3, ... at 24 -> 60 fps),
    interpolating each step over its own hold makes an object move faster on the short holds than on
    the long ones. That alone is a slight judder, but nested clips that turn opposite ways (Gwent's
    fog: +7.7 and -8 degrees per frame) fall out of step with each other as they loop, and their
    small net turn then lurches back and forth by ten times its size. So over a run of steps the
    object moves at an even pace, tick by tick, and passes its keyframe states in between ticks
    rather than on them. Runs end at anchor frames (where playback may start, stop or jump to), and at
    the wrap back to frame 0, whose states are shown exactly: ticks there get no sample."""
    steps_by_depth = defaultdict(dict)  # depth -> {frame: step from that frame to the next}
    for frame, frame_steps in steps.items():
        for step in frame_steps:
            steps_by_depth[step.depth][frame] = step

    samples = defaultdict(list)
    for depth, depth_steps in sorted(steps_by_depth.items()):
        for start, end in _runs(sorted(depth_steps), anchors):
            first_tick, last_tick = retiming.tick(start), retiming.tick(end)
            for tick in range(first_tick + 1, last_tick):
                time = start + Fraction((tick - first_tick) * (end - start), last_tick - first_tick)
                frame = math.floor(time)
                samples[tick].append(TweenSample(depth_steps[frame], time - frame))
    return dict(samples)


def _runs(frames: list[int], anchors: set[int]) -> list[tuple[int, int]]:
    """(start, end) frames of each run of consecutive steps (from frame k to k + 1, for each k in
    frames), split at anchor frames"""
    runs = []
    for frame in frames:
        if runs and runs[-1][1] == frame and frame not in anchors:
            runs[-1] = (runs[-1][0], frame + 1)
        else:
            runs.append((frame, frame + 1))
    return runs


def tween_moves(tags, samples: dict[int, list[TweenSample]], retiming: Retiming) -> dict[int, list[Tag]]:
    """The moves that put the tweened objects where samples says, as {tick: PlaceObject tags}. On a
    keyframe's tick, they come after its own tags. They only set what differs from the object's
    state at that point, and restate its display options (filters etc.), as Flash does on every
    frame of a tween: Scaleform appears to drop those on moves that don't."""
    display, moves, frame = DisplayList(), defaultdict(list), 0

    def place(tick):
        for sample in sorted(samples.get(tick, ()), key=lambda sample: sample.depth):
            current = display.objects[sample.depth]
            target = interpolate(sample.step.start, sample.step.end, float(sample.t))
            matrix, cxform, ratio = (getattr(target, field) if getattr(target, field) != getattr(current, field)
                                     else None for field in ("matrix", "color_transform", "ratio"))
            if matrix or cxform or ratio is not None:
                move = build_move(sample.depth, matrix, cxform, ratio, current.display_options)
                display.apply(move)
                moves[tick].append(move)

    for tag in tags:
        if tag.code != TagCode.SHOW_FRAME:
            display.apply(tag)
            continue
        for tick in range(retiming.tick(frame), retiming.tick(frame + 1)):
            place(tick)
        frame += 1
    return dict(moves)
