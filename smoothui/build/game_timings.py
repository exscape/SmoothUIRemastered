"""Writing patcher/game_timings.txt from the uncooked game files: their frame rates, and their clips' frame
counts by name and timing patterns (see patcher/game_timing.py). build-main.py runs it before every build, so
it can't be stale.
"""

from __future__ import annotations

import logging
import struct
from collections import defaultdict

from ..common import fail
from ..patcher.game_timing import DATA_FILE, FORMAT_LINE, clip_name, timing_pattern
from ..patcher.swf import FPS_FIXED_POINT_SCALE, swf_get_header_offsets, swf_patch
from ..patcher.swf_patcher.swf_tags import (
    TagCode,
    parse_sprite_header,
    parse_sprite_tags,
    parse_symbol_class,
    parse_tags,
)
from ..witcher3.redswf import RedSwf, RedSwfError

log = logging.getLogger(__name__)

UI_FOLDER = "gameplay/gui_new"


def game_timings_text(uncooked_dir) -> str:
    """DATA_FILE's contents, for the game's .redswf files in uncooked_dir"""
    fps_lines, frames, patterns = [], defaultdict(set), set()
    files = sorted((uncooked_dir / UI_FOLDER).rglob("*.redswf"))
    if not files:
        fail(f"no .redswf files in {uncooked_dir / UI_FOLDER}")
    for path in files:
        try:
            fps, clips = _read(RedSwf(path.read_bytes()).swf)
        except (OSError, RedSwfError, ValueError) as e:
            fail(f"can't read {path}: {e}")
        fps_lines.append(f"fps {path.relative_to(uncooked_dir).as_posix().lower()} {fps:g}")
        for class_name, frame_count, pattern in clips:
            if class_name:
                frames[clip_name(class_name)].add(frame_count)
            patterns.add(pattern)
    frame_lines = [f"frames {name} {','.join(map(str, sorted(counts)))}" for name, counts in sorted(frames.items())]
    return "\n".join([FORMAT_LINE, *fps_lines, *frame_lines, *(f"timing {pattern}" for pattern in sorted(patterns))]) + "\n"


def update_game_timings(uncooked_dir, *, dry_run=False):
    """Write DATA_FILE from uncooked_dir, if it changed (dry run: only say so)"""
    text = game_timings_text(uncooked_dir)
    try:
        old = DATA_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        old = None
    if old == text:
        log.info(f"{DATA_FILE.name} is up to date")
    elif dry_run:
        log.warning(f"DRY RUN: {DATA_FILE.name} is out of date; a build would write it")
    else:
        DATA_FILE.write_text(text, encoding="utf-8", newline="\n")
        log.info(f"Wrote {DATA_FILE.name}: the game files changed" if old else f"Wrote {DATA_FILE.name}")


def _read(swf):
    """(header frame rate, [(class name or None, frame count, timing pattern)] of its clips with more than one
    frame) of an SWF"""
    found = []

    def read(body):
        fps_offset, first_tag = swf_get_header_offsets(body)
        if fps_offset is None:
            raise ValueError("not a valid SWF")
        tags = parse_tags(body, first_tag)
        classes = dict(pair for tag in tags if tag.code == TagCode.SYMBOL_CLASS for pair in parse_symbol_class(tag))
        clips = []
        for tag in tags:
            if tag.code != TagCode.DEFINE_SPRITE:
                continue
            character_id, _ = parse_sprite_header(tag)
            sprite_tags = parse_sprite_tags(tag)
            frame_count = sum(sub.code == TagCode.SHOW_FRAME for sub in sprite_tags)
            if frame_count > 1:
                clips.append((classes.get(character_id), frame_count, timing_pattern(sprite_tags)))
        found.append((struct.unpack_from("<H", body, fps_offset)[0] / FPS_FIXED_POINT_SCALE, clips))
        return False, None

    if swf_patch(swf, 0, read) is None or not found:
        raise ValueError("no SWF found")
    return found[0]
