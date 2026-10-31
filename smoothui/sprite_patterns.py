"""Include/exclude patterns for sprites, as written in profiles and reviews. Light: the launcher's reviews parser
needs them, and it doesn't have the patcher"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpriteRef:
    """A sprite by character id ("sprite N" in profiles), for include/exclude.

    If expected is given, as (class name or None, frame count), the sprite must still match it;
    otherwise the reference matches nothing, and is reported as an unmet rule. Compat builds use
    this for ids recorded for the release, as the other mod's version of the SWF may differ."""
    id: int
    expected: tuple[str | None, int] | None = None

    def __str__(self) -> str:
        return f"sprite {self.id}"


@dataclass(frozen=True, slots=True)
class InstanceRef:
    """The sprites placed with an instance name below the sprites of a class ("Class/instanceName" in
    profiles), at any depth, for include/exclude: for sprites without a class of their own, by a name that
    stays the same when the file changes (unlike a sprite id)"""
    class_name: str
    instance: str

    def __str__(self) -> str:
        return f"{self.class_name}/{self.instance}"


def parse_sprite_pattern(text: str) -> str | SpriteRef | InstanceRef:
    """An include/exclude pattern as written in a profile: "sprite N", "Class/instanceName", or a class
    name regex"""
    if match := re.fullmatch(r"sprite (\d+)", text):
        return SpriteRef(int(match[1]))
    if match := re.fullmatch(r"([\w.$]+)/(\w+)", text):     # (a class name has no "/")
        return InstanceRef(match[1], match[2])
    if text.isdigit():
        raise ValueError(f"'{text}' is not a class name; write 'sprite {text}' for a sprite id")
    try:
        re.compile(text)
    except re.error as e:
        raise ValueError(f"invalid regex '{text}': {e}") from None
    return text
