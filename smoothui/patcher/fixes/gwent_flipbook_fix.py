"""gwint_game: the card effects' flipbooks play at the game's 24 fps.

The smoke, fire and sparks of the card effects, and the breaking gem, are flipbooks: sprites that show a
different picture (a shape) on every frame. The rescale can't interpolate a swap of pictures, so they step
at 24 fps amid smooth motion. This fix rebuilds their timelines so that every tick between two frames shows
a mix of both: a crossfade.

Scaleform can add colours (blend mode add) but not alphas: add leaves the alpha as it is, and a normal blend
multiplies what the pictures let through, so a picture fading in over one fading out hides more or less of
what is behind them mid-fade than either does alone, a 24 Hz pulse. So a mix has two parts:

- the colour: an add-mode copy of each frame, at its weight
- the alpha (how much of what is behind gets hidden): a "darkener" sprite underneath. In it, each frame is
  placed under a ColorMatrix filter that writes its alpha to a colour channel of its own and nothing else
  (alpha 0). Scaleform, unlike Flash, draws the colour of transparent pixels, so those results are added
  into the darkener's own filter texture as they are: it holds the frames' alphas side by side. The
  darkener's ColorMatrix then makes their weighted sum the alpha, with no colour: black at exactly the mixed
  alpha, also inside the filter texture of an effect that puts a filter on its flipbook.

Every tick is drawn this way, the frames themselves too, so that none looks different from the rest.

A crossfade adds no motion, so where the pictures show something moving steadily, they are moved as well, on
every tick (_grow, _rise).

Only the game's own flipbooks are changed, told by a fingerprint of their pictures (KNOWN): what suits a
flipbook depends on what it shows. A flipbook is known by how many pictures it has, where it puts them, and
each picture's bounds and fills (colours, gradients, or the bitmap and where it is drawn). Sprite, shape and
bitmap numbers don't matter, and neither do the pictures' edges: other exports of the file (mods' copies
of it) store the same edges in other orders and directions, and to compare them all would be slow. A
flipbook with a picture added, removed, or changed in those is left as it is. More than one flipbook that
isn't the game's means that the file isn't the game's as the fix knows it: then it changes nothing.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from bisect import bisect_right
from collections.abc import Callable
from itertools import pairwise
from typing import NamedTuple

from ..swf_patcher.bits import BitReader, BitWriter, signed_bits_needed
from ..swf_patcher.interpolation import simulate_timeline
from ..swf_patcher.place_object import (
    BLEND_ADD,
    FIXED_ONE,
    IDENTITY_COLOR_TRANSFORM,
    IDENTITY_MATRIX,
    PLACE_OBJECT_CODES,
    DisplayOption,
    FilterId,
    Matrix,
    build_move,
    build_place,
    parse_place_object,
    read_matrix,
)
from ..swf_patcher.swf_tags import (
    REMOVE_OBJECT_CODES,
    Tag,
    TagCode,
    build_sprite,
    parse_remove_object_depth,
)
from .swf_edit import CantApply, Fix, FixKind, FixResult, Phase, SwfEdit

log = logging.getLogger(__name__)

SHAPE_CODES = (TagCode.DEFINE_SHAPE, TagCode.DEFINE_SHAPE2, TagCode.DEFINE_SHAPE3, TagCode.DEFINE_SHAPE4)
SOLID_FILL, BITMAP_FILLS = 0x00, (0x40, 0x41, 0x42, 0x43)
GRADIENT_FILLS, FOCAL_GRADIENT = (0x10, 0x12, 0x13), 0x13
FLIPBOOK_CODES = {*PLACE_OBJECT_CODES, *REMOVE_OBJECT_CODES, TagCode.SHOW_FRAME, TagCode.FRAME_LABEL, TagCode.END}
MIN_SWAPS = 5                   # a flipbook swaps its picture at least this often
FLIP = 1                        # the depth a flipbook shows its pictures at
DARKENER, COLOUR = 1, 3         # the new timeline's depths: the darkener; frame j's colour at COLOUR + j % 3
ADD = ((DisplayOption.BLEND_MODE, bytes([BLEND_ADD])),)
TWIPS = 20
MARGIN = 10 * TWIPS             # around a flipbook's bounds, for what the motion moves outward


class Frame(NamedTuple):
    """One picture of a flipbook"""
    shape: int
    matrix: Matrix
    box: tuple[int, int, int, int]      # the shape's bounds in twips: x min, x max, y min, y max


def _still(frames: list[Frame], j: int, i: int, t: float) -> Matrix:
    """Where frame j is drawn while frame i is t of the way to frame i + 1: where the flipbook puts it"""
    return frames[j].matrix


def _grow(frames: list[Frame], j: int, i: int, t: float) -> Matrix:
    """The gem: it swells, then its shards fly straight out. Where the picture's bounds grow from frame i to
    i + 1, frame i is stretched from its own bounds to those of both, and frame i + 1 from frame i's bounds to
    its own, so that both copies move outward together. Outward only: an edge that moves inward is mostly
    something fading."""
    if i + 1 == len(frames):
        return frames[j].matrix
    box, next_box = frames[i].box, frames[i + 1].box
    grown = (min(box[0], next_box[0]), max(box[1], next_box[1]), min(box[2], next_box[2]), max(box[3], next_box[3]))
    now = tuple(edge + (grown_edge - edge) * t for edge, grown_edge in zip(box, grown, strict=True))
    return _stretched(frames[j].matrix, box if j == i else grown, now)


# Muster's arrival: its stars rise. How far they move from each frame to the next, in pixels (x, y): for each
# pair of frames as the game draws them, the shift at which the first best matches the second
_MEASURED_STEPS = tuple(zip(
    (-3, -3, -3, -4, -3, -3, -4, -4, -2, -1, -2, 1, 2, 2, 2, 3, 2, 5, 3, 3, 3, 3, 2, 1, 3, 3, 3, 3, 3, 3, 3, 4, 3, 2, 3,
     3, 2, 0, 0),
    (-4, -5, -6, -5, -6, -6, -6, -5, -6, -6, -6, -6, -6, -6, -6, -6, -6, -5, -5, -5, -4, -5, -3, -5, -4, -3, -3, -3,
     -3, -3, -5, -6, -6, -6, -6, -6, -5, 0, -1), strict=True))


def _path(measured) -> tuple[list, list]:
    """(the steps, each averaged with its neighbours; where each frame is along them, then: the first at 0, 0)"""
    ends = [measured[0], *measured, measured[-1]]
    steps = [tuple((a[c] + 2 * b[c] + d[c]) / 4 for c in (0, 1)) for a, b, d in zip(ends, ends[1:], ends[2:])]
    path = [(0.0, 0.0)]
    for dx, dy in steps:
        path.append((path[-1][0] + dx, path[-1][1] + dy))
    return steps, path


MUSTER_STEPS, MUSTER_PATH = _path(_MEASURED_STEPS)


def _rise(frames: list[Frame], j: int, i: int, t: float) -> Matrix:
    """Muster's arrival: every frame is shifted along the stars' path, from where they are on that frame to
    where they are now, so that they rise on every tick"""
    step = MUSTER_STEPS[i] if i < len(MUSTER_STEPS) else (0, 0)
    x, y = (MUSTER_PATH[i][c] + t * step[c] - MUSTER_PATH[j][c] for c in (0, 1))
    base = frames[j].matrix
    return base._replace(translate_x=base.translate_x + round(TWIPS * x),
                         translate_y=base.translate_y + round(TWIPS * y))


class Known(NamedTuple):
    """One of the game's flipbooks, and how it is crossfaded"""
    name: str                   # for the fix's report
    hard_cuts: int = 0          # how many of its first frames don't fade into the next
    motion: Callable[[list[Frame], int, int, float], Matrix] = _still


# The game's flipbooks (5.01) by fingerprint (see _fingerprint); None: left as it is
KNOWN = {
    "cba86a3b6526e8bd": Known("Deploy", hard_cuts=3),   # the hit at its start stays hard
    "0401387425ffaa74": Known("Clear Weather"),
    "65f4d71d90f24802": Known("Rain"),
    "cf5608c291e06069": None,   # the rain's drops: 32 on screen at once, each with little to gain
    "2992a9e2ba5f851c": Known("Scorch"),
    "ee028a4d383286e9": Known("Scorch"),
    "41e7426167bc0bf4": Known("Decoy"),
    "8ebf6724c1861c86": Known("Muster and Medic"),
    "df917e38a4732e3a": Known("Muster's arrival", motion=_rise),
    "be854042df2b7594": Known("Hero"),
    "9884512cdf759703": Known("the puff of Hero, Spy and Morph"),
    "227fa6807c6ea5f7": Known("a gem breaking", motion=_grow),
}


def apply(swf: SwfEdit) -> FixResult:
    shapes = {struct.unpack_from("<H", tag.payload)[0]: tag for tag in swf.tags if tag.code in SHAPE_CODES}
    found, unknown = [], []
    for sprite_id in swf.sprite_ids():
        tags = swf.sprite_tags(sprite_id)
        assert tags is not None     # an id from sprite_ids()
        if not _is_flipbook(tags, shapes):
            continue
        snaps, keys, frames = _frames(tags, shapes)
        fingerprint = _fingerprint(frames, shapes)
        if fingerprint in KNOWN:
            found.append((sprite_id, tags, snaps, keys, frames, KNOWN[fingerprint]))
        else:
            log.warning(f"{FIX.name}: sprite {sprite_id} is a flipbook that isn't one of the game's "
                        f"({len(frames)} frames, fingerprint {fingerprint})")
            unknown.append(sprite_id)
    ids = ", ".join(map(str, unknown))
    if len(unknown) > 1 or not found:
        raise CantApply(f"{len(unknown)} flipbook(s) that aren't the game's ({ids})" if unknown
                        else "found none of the game's flipbooks")
    done = []
    for sprite_id, tags, snaps, keys, frames, known in found:
        if known is None or all(b - a == 1 for a, b in pairwise(keys)):     # or not stretched
            continue
        characters = _Characters(swf, frames)
        new_tags = _crossfade(tags, snaps, keys, frames, known, characters)
        swf.insert_before_sprite(sprite_id, characters.tags)
        swf.replace_sprite(sprite_id, new_tags)
        done.append((sprite_id, known.name))
    if not done:
        return FixResult([], "the flipbooks aren't stretched, or are crossfaded already")
    return FixResult([sprite_id for sprite_id, _ in done],
                     "crossfaded " + ", ".join(f"{name} ({sprite_id})" for sprite_id, name in done)
                     + (f"; 1 flipbook that isn't the game's left alone ({ids})" if unknown else ""))


def _is_flipbook(tags: list[Tag], shapes: dict[int, Tag]) -> bool:
    """Whether a timeline only puts shapes at depth FLIP, as they are, one after the other"""
    if {tag.code for tag in tags} - FLIPBOOK_CODES:
        return False
    places = [parse_place_object(tag) for tag in tags if tag.code in PLACE_OBJECT_CODES]
    removes = {parse_remove_object_depth(tag) for tag in tags if tag.code in REMOVE_OBJECT_CODES}
    return (removes <= {FLIP} and sum(place.is_move for place in places) >= MIN_SWAPS
            and all(place.depth == FLIP and place.character_id in shapes and not place.name
                    and not place.display_options and place.color_transform is None
                    and not (place.is_move and place.matrix) for place in places))


def _frames(tags: list[Tag], shapes: dict[int, Tag]) -> tuple[list, list[int], list[Frame]]:
    """A flipbook's display list on every tick, the ticks its frames start on, and the frames"""
    snaps = simulate_timeline(tags)
    keys = [k for k, snap in enumerate(snaps) if FLIP in snap and (
        k == 0 or FLIP not in snaps[k - 1] or snaps[k - 1][FLIP].instance != snap[FLIP].instance)]
    return snaps, keys, [Frame(snaps[k][FLIP].character_id, snaps[k][FLIP].matrix,
                               _shape_box(shapes[snaps[k][FLIP].character_id])) for k in keys]


def _fingerprint(frames: list[Frame], shapes: dict[int, Tag]) -> str:
    """What tells a flipbook: its pictures, in order, and where it puts them"""
    digest = hashlib.sha256()
    for frame in frames:
        key = _shape_key(shapes[frame.shape])
        digest.update(struct.pack("<I6i", len(key), *frame.matrix) + key)
    return digest.hexdigest()[:16]


def _after_rect(data: bytes | bytearray, offset: int) -> int:
    bits = BitReader(data, offset)
    width = bits.read(5)
    for _ in range(4):
        bits.read(width)
    return bits.align()


def _shape_key(tag: Tag) -> bytes:
    """A shape's definition up to its edges: its type, bounds, and fill and line styles, less its character
    id and those of the bitmaps it is filled with. Its edges are left out: exports of the same picture store
    them in other orders and directions, and to read them all would be slow. A shape whose styles it can't
    read is taken as it is, less its id."""
    data = bytearray(tag.payload)
    data[0:2] = b"\0\0"
    try:
        offset = _after_rect(data, 2)
        if tag.code == TagCode.DEFINE_SHAPE4:   # the edge bounds and a flags byte
            offset = _after_rect(data, offset) + 1
        data = data[:_skip_styles(data, offset, tag.code)]
    except (IndexError, ValueError, struct.error):
        pass
    return struct.pack("<H", tag.code) + data


def _skip_styles(data: bytearray, offset: int, code: int) -> int:
    """Skip the fill and line style arrays at offset, zeroing the ids of the bitmaps they fill with; the offset
    after them"""
    colour = 4 if code in (TagCode.DEFINE_SHAPE3, TagCode.DEFINE_SHAPE4) else 3     # RGBA or RGB
    for line_styles in (False, True):
        count, offset = data[offset], offset + 1
        if count == 0xFF and code != TagCode.DEFINE_SHAPE:
            count, offset = struct.unpack_from("<H", data, offset)[0], offset + 2
        for _ in range(count):
            if not line_styles:
                offset = _skip_fill(data, offset, colour)
            elif code == TagCode.DEFINE_SHAPE4:
                # A width, two bytes of flags, a miter limit if the join style is miter, a colour or a fill
                flags = data[offset + 2]
                offset += 4 + (2 if (flags >> 4) & 3 == 2 else 0)
                offset = _skip_fill(data, offset, colour) if flags & 0x08 else offset + colour
            else:
                offset += 2 + colour    # a width and a colour
    return offset


def _skip_fill(data: bytearray, offset: int, colour: int) -> int:
    kind, offset = data[offset], offset + 1
    if kind == SOLID_FILL:
        return offset + colour
    if kind in GRADIENT_FILLS:
        offset = read_matrix(data, offset)[1]
        count = data[offset] & 0x0F
        return offset + 1 + count * (1 + colour) + (2 if kind == FOCAL_GRADIENT else 0)
    if kind in BITMAP_FILLS:
        data[offset:offset + 2] = b"\0\0"
        return read_matrix(data, offset + 2)[1]
    raise ValueError(f"unknown fill style {kind:#x}")


def _shape_box(tag: Tag) -> tuple[int, int, int, int]:
    """A shape's bounds in twips: x min, x max, y min, y max"""
    bits = BitReader(tag.payload, 2)
    width = bits.read(5)
    x_min, x_max, y_min, y_max = (bits.read_signed(width) for _ in range(4))
    return x_min, x_max, y_min, y_max


def _placed_box(box, m: Matrix) -> tuple[int, int, int, int]:
    """A box after a matrix"""
    x0, x1, y0, y1 = box
    xs, ys = [], []
    for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        xs.append((x * m.scale_x + y * m.rotate_skew1) / FIXED_ONE + m.translate_x)
        ys.append((x * m.rotate_skew0 + y * m.scale_y) / FIXED_ONE + m.translate_y)
    return int(min(xs)), int(max(xs)) + 1, int(min(ys)), int(max(ys)) + 1


def _stretched(base: Matrix, source, target) -> Matrix:
    """base, after first stretching the shape so that its `source` box lands on `target`"""
    ax = (target[1] - target[0]) / (source[1] - source[0])
    ay = (target[3] - target[2]) / (source[3] - source[2])
    bx, by = target[0] - ax * source[0], target[2] - ay * source[2]
    return Matrix(scale_x=round(base.scale_x * ax), scale_y=round(base.scale_y * ay),
                  rotate_skew0=round(base.rotate_skew0 * ax), rotate_skew1=round(base.rotate_skew1 * ay),
                  translate_x=round((bx * base.scale_x + by * base.rotate_skew1) / FIXED_ONE + base.translate_x),
                  translate_y=round((bx * base.rotate_skew0 + by * base.scale_y) / FIXED_ONE + base.translate_y))


def _colour_matrix(**rows) -> tuple:
    """A filter list of one ColorMatrix, as a display option. rows: r, g, b or a = that output channel's
    factors for the input's r, g, b and a; everything else is 0."""
    values = [0.0] * 20
    for channel, factors in rows.items():
        at = "rgba".index(channel) * 5
        values[at:at + 4] = factors
    return ((DisplayOption.FILTER_LIST, bytes([1, FilterId.COLOR_MATRIX]) + struct.pack("<20f", *values)),)


def _rect_shape(character_id: int, box) -> Tag:
    """A DefineShape: an opaque black rectangle"""
    x0, x1, y0, y1 = box
    bounds = BitWriter()
    width = signed_bits_needed(box)
    bounds.write(width, 5)
    for edge in box:
        bounds.write_signed(edge, width)
    bits = BitWriter()
    bits.write(1, 4)            # fill style index bits
    bits.write(0, 4)            # line style index bits
    width = signed_bits_needed((x0, y0))
    bits.write(0b000101, 6)     # style change: fill style 1 (the fill to the right of the edges), move to
    bits.write(width, 5)
    bits.write_signed(x0, width)
    bits.write_signed(y0, width)
    bits.write(1, 1)
    for delta, vertical in ((x1 - x0, 0), (y1 - y0, 1), (x0 - x1, 0), (y0 - y1, 1)):   # clockwise
        width = max(signed_bits_needed((delta,)), 2)
        bits.write(0b11, 2)     # straight edge
        bits.write(width - 2, 4)
        bits.write(0, 1)        # not a general line
        bits.write(vertical, 1)
        bits.write_signed(delta, width)
    bits.write(0, 6)            # end
    # One fill style (solid, black), no line styles
    return Tag(TagCode.DEFINE_SHAPE, struct.pack("<H", character_id) + bounds.to_bytes() + bytes([1, SOLID_FILL, 0, 0, 0, 0])
               + bits.to_bytes())


class _Characters:
    """The characters a flipbook's new timeline needs, defined before it in the file: a black rectangle over
    its bounds, a sprite around each shape (which can then take a filter), and the darkeners"""

    def __init__(self, swf: SwfEdit, frames: list[Frame]):
        self.swf = swf
        self.tags, self.wrappers, self.darkeners = [], {}, {}
        boxes = [_placed_box(frame.box, frame.matrix) for frame in frames]
        self.rect = swf.new_character_id()
        self.tags.append(_rect_shape(self.rect, (min(box[0] for box in boxes) - MARGIN,
                                                 max(box[1] for box in boxes) + MARGIN,
                                                 min(box[2] for box in boxes) - MARGIN,
                                                 max(box[3] for box in boxes) + MARGIN)))

    def _sprite(self, places: list[Tag]) -> int:
        character_id = self.swf.new_character_id()
        self.tags.append(build_sprite(character_id, 1, [*places, Tag(TagCode.SHOW_FRAME, b""), Tag(TagCode.END, b"")]))
        return character_id

    def darkener(self, parts: tuple) -> int:
        """The darkener of a mix; parts: (channel 0-2, shape, matrix) of each of its frames. The rectangle
        gives its texture an alpha of 1 everywhere, whatever a ColorMatrix does with premultiplied colour."""
        if parts not in self.darkeners:
            places = [build_place(1, self.rect)]
            for channel, shape, matrix in parts:
                if shape not in self.wrappers:
                    self.wrappers[shape] = self._sprite([build_place(2, shape, IDENTITY_MATRIX)])
                places.append(build_place(2 + channel, self.wrappers[shape], matrix,
                                          display_options=_colour_matrix(**{"rgb"[channel]: (0, 0, 0, 1)})))
            self.darkeners[parts] = self._sprite(places)
        return self.darkeners[parts]


def _crossfade(tags: list[Tag], snaps: list, keys: list[int], frames: list[Frame], known: Known,
               characters: _Characters) -> list[Tag]:
    """A flipbook's timeline, rebuilt (snaps, keys, frames: see _frames)"""
    labels, tick = {}, 0
    for tag in tags:
        if tag.code == TagCode.SHOW_FRAME:
            tick += 1
        elif tag.code == TagCode.FRAME_LABEL:
            labels.setdefault(tick, []).append(tag)
    # fades[i]: frame i fades into frame i + 1
    fades = [i >= known.hard_cuts and all(FLIP in snaps[k] for k in range(a, b))
             for i, (a, b) in enumerate(pairwise(keys))]
    remove = lambda depth: Tag(TagCode.REMOVE_OBJECT2, struct.pack("<H", depth))
    out, before = [], {}
    for k, snap in enumerate(snaps):
        state = {}      # depth -> (character, matrix, colour transform, display options)
        if FLIP in snap:
            i = bisect_right(keys, k) - 1
            fading = i < len(fades) and fades[i]
            t = (k - keys[i]) / (keys[i + 1] - keys[i]) if fading else 0
            weight = round(256 * t)
            # On a frame's first tick the next is in the mix at weight 0: only in the darkener, which then
            # lasts the whole hold
            mix = [(i, 256 - weight), *([(i + 1, weight)] if fading else [])]
            factors, parts = [0.0] * 4, []
            for j, share in mix:    # a frame keeps its channel and depth (j % 3) for as long as it shows
                matrix = known.motion(frames, j, i, t)
                parts.append((j % 3, frames[j].shape, matrix))
                factors[j % 3] = share / 256
                if share:
                    state[COLOUR + j % 3] = (frames[j].shape, matrix,
                                             IDENTITY_COLOR_TRANSFORM._replace(mul_a=share), ADD)
            state[DARKENER] = (characters.darkener(tuple(parts)), IDENTITY_MATRIX, IDENTITY_COLOR_TRANSFORM,
                               _colour_matrix(a=factors))
        out += labels.get(k, [])
        out += [remove(depth) for depth in sorted(before.keys() - state.keys())]
        for depth, (character, matrix, cxform, options) in sorted(state.items()):
            old = before.get(depth)
            if old and character != old[0]:     # placed anew rather than swapped in: it may be a sprite
                out.append(remove(depth))
                old = None
            if old is None:
                out.append(build_place(depth, character, matrix, cxform, options))
            elif old != state[depth]:
                out.append(build_move(depth, matrix if matrix != old[1] else None,
                                      cxform if cxform != old[2] else None,
                                      display_options=options if options != old[3] else ()))
        out.append(Tag(TagCode.SHOW_FRAME, b""))
        before = state
    out.append(Tag(TagCode.END, b""))
    return out


FIX = Fix(name="gwent-flipbook-fix",
          kind=FixKind.SMOOTHUI_FIX,
          consequence="in Gwent, the smoke, fire and sparks of card effects play at 24 fps",
          apply=apply,
          # The flipbooks must be stretched: the fix fills the ticks the rescale adds between their frames
          phase=Phase.AFTER_RESCALE)
