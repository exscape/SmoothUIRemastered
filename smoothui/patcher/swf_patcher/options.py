"""Settings for rescaling one SWF"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FpsRule:
    """Retime the sprites whose linkage class name matches `pattern` to `fps` instead of the
    file's target frame rate.

    The sprite's frames are held as if the whole file were going to `fps`, while the file itself
    still plays at the target rate. A rule below the target therefore speeds the sprite up (at 120 fps,
    a sprite retimed to 80 fps plays 1.5x as fast as the original), and one above it slows it down.
    A rule at or below the file's original frame rate leaves the sprite unstretched.

    The pattern is matched with re.search, ignoring case, unless it is passed pre-compiled."""
    pattern: str | re.Pattern
    fps: float

    def __post_init__(self):
        if isinstance(self.pattern, str):
            object.__setattr__(self, "pattern", re.compile(self.pattern, re.IGNORECASE))
        if self.fps <= 0:
            raise ValueError(f"invalid fps {self.fps} in rule {self.pattern.pattern!r}")

    @property
    def description(self) -> str:
        return f"fps rule '{self.pattern.pattern}' ({self.fps:g} fps)"

    def matches(self, class_name: str) -> bool:
        return bool(class_name) and self.pattern.search(class_name) is not None


def make_rules(rules) -> tuple[FpsRule, ...]:
    """FpsRules from FpsRules and/or (pattern, fps) pairs"""
    return tuple(rule if isinstance(rule, FpsRule) else FpsRule(*rule) for rule in rules)


@dataclass(frozen=True)
class RescaleOptions:
    target_fps: float
    # FpsRules or (pattern, fps) pairs; the first matching rule applies
    rules: tuple[FpsRule, ...] = ()
    # Sprite ids (as digit strings) or regexes matched against the linkage class name: never stretch
    # these / stretch these despite the automatic exclusions
    exclude: tuple = ()
    include: tuple = ()
    # Also stretch the clips that are only excluded because an untyped value is passed to
    # gotoAndStop/gotoAndPlay, but whose labels suggest that the value is a label name
    include_likely: bool = False
    # Rescale per-frame `obj.prop += K` in persistent ENTER_FRAME handlers
    scale_handlers: bool = True
    # Don't interpolate steps smaller than this (in roughly pixel-sized units)
    min_motion: float = 0.5

    def __post_init__(self):
        object.__setattr__(self, "rules", make_rules(self.rules))

    def fps_rule_for(self, class_name: str) -> FpsRule | None:
        return next((rule for rule in self.rules if rule.matches(class_name)), None)
