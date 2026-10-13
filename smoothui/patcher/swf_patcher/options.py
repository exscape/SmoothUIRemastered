"""Settings for rescaling one SWF"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FpsRule:
    """Retime the sprites whose linkage class name matches `pattern` to `fps` instead of the
    file's target frame rate.

    The sprite's frames are held as if the whole file were going to `fps`, while the file itself
    still plays at the target rate, so the sprite plays at target / fps times its original speed.
    The lowest possible `fps` is the file's original frame rate (as stored in the unpatched SWF),
    where no frames are repeated; lower would need frames dropped, which is not supported.

    The pattern is matched against the class name with re.search, case-sensitively."""
    pattern: str | re.Pattern
    fps: float

    def __post_init__(self):
        if isinstance(self.pattern, str):
            object.__setattr__(self, "pattern", re.compile(self.pattern))
        if self.fps <= 0:
            raise ValueError(f"invalid fps {self.fps} in rule {self.pattern.pattern!r}")

    @property
    def description(self) -> str:
        """As written in a profile"""
        return f"retime {self.fps:g}: {self.pattern.pattern}"

    def matches(self, class_name: str) -> bool:
        return bool(class_name) and self.pattern.search(class_name) is not None


@dataclass(frozen=True)
class SpriteRef:
    """A sprite by character id ("sprite N" in profiles), for include/exclude.

    If expected is given, as (class name or None, frame count), the sprite must still match it;
    otherwise the reference matches nothing, and is reported as an unmet rule. Compat builds use
    this for ids recorded for the release, as the other mod's version of the SWF may differ."""
    id: int
    expected: tuple[str | None, int] | None = None

    def __str__(self) -> str:
        return f"sprite {self.id}"


def parse_sprite_pattern(text: str) -> str | SpriteRef:
    """An include/exclude pattern as written in a profile: "sprite N", or a class name regex"""
    if match := re.fullmatch(r"sprite (\d+)", text):
        return SpriteRef(int(match[1]))
    if text.isdigit():
        raise ValueError(f"'{text}' is not a class name; write 'sprite {text}' for a sprite id")
    try:
        re.compile(text)
    except re.error as e:
        raise ValueError(f"invalid regex '{text}': {e}") from None
    return text


def make_rules(rules) -> tuple[FpsRule, ...]:
    """FpsRules from FpsRules and/or (pattern, fps) pairs"""
    return tuple(rule if isinstance(rule, FpsRule) else FpsRule(*rule) for rule in rules)


@dataclass(frozen=True)
class RescaleOptions:
    target_fps: float
    # FpsRules or (pattern, fps) pairs; the first matching rule applies
    rules: tuple[FpsRule, ...] = ()
    # SpriteRefs, or regexes matched (case-sensitively) against the linkage class name: never stretch
    # these / stretch these despite the automatic exclusions
    exclude: tuple = ()
    include: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "rules", make_rules(self.rules))

    def fps_rule_for(self, class_name: str) -> FpsRule | None:
        return next((rule for rule in self.rules if rule.matches(class_name)), None)
