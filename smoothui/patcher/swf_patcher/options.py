"""Settings for rescaling one SWF"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction


def parse_speed(text: str) -> Fraction:
    """A speed as written in a profile: a decimal or a fraction ("1.5", "3/2"), kept exact"""
    try:
        speed = Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"invalid speed '{text}': expected a number such as 1.5 or 3/2") from None
    if speed <= 0:
        raise ValueError(f"invalid speed '{text}': must be above 0")
    return speed


def format_speed(speed: Fraction) -> str:
    """A speed as a profile would give it: as a decimal where that is exact (2, 1.5, 0.75), otherwise
    as a fraction (4/3). parse_speed reads it back exactly."""
    denominator = speed.denominator
    while denominator % 2 == 0:
        denominator //= 2
    while denominator % 5 == 0:
        denominator //= 5
    if denominator != 1:
        return str(speed)
    places = 0
    while (speed * 10**places).denominator != 1:
        places += 1
    return f"{float(speed):.{places}f}"


@dataclass(frozen=True)
class SpeedRule:
    """Play the sprites whose linkage class name matches `pattern` at `speed` times their original
    speed (1 is what every other sprite gets), whatever the target frame rate.

    The sprite's frames are held as if the whole file were going to target fps / speed, while the
    file itself plays at the target rate. A speed below 1 slows the sprite down. The highest possible
    speed is target fps / the file's original frame rate, where no frames are repeated; higher would
    need frames dropped, which is not supported.

    The pattern is matched against the class name with re.search, case-sensitively."""
    pattern: str | re.Pattern
    speed: Fraction

    def __post_init__(self):
        if isinstance(self.pattern, str):
            object.__setattr__(self, "pattern", re.compile(self.pattern))
        if not isinstance(self.speed, Fraction):
            object.__setattr__(self, "speed", parse_speed(str(self.speed)))
        if self.speed <= 0:
            raise ValueError(f"invalid speed {self.speed} in rule {self.pattern.pattern!r}")

    @property
    def description(self) -> str:
        """As written in a profile (and in report_settings)"""
        return speed_rule_text(self.speed, self.pattern.pattern)

    def matches(self, class_name: str) -> bool:
        return bool(class_name) and self.pattern.search(class_name) is not None


def speed_rule_text(speed: Fraction, pattern: str) -> str:
    """A speed rule as written in a profile"""
    return f"speed {format_speed(speed)}: {pattern}"


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


def make_rules(rules) -> tuple[SpeedRule, ...]:
    """SpeedRules from SpeedRules and/or (pattern, speed) pairs"""
    return tuple(rule if isinstance(rule, SpeedRule) else SpeedRule(*rule) for rule in rules)


@dataclass(frozen=True)
class RescaleOptions:
    target_fps: float
    # SpeedRules or (pattern, speed) pairs; the first matching rule applies
    rules: tuple[SpeedRule, ...] = ()
    # SpriteRefs, or regexes matched (case-sensitively) against the linkage class name: never stretch
    # these / stretch these despite the automatic exclusions
    exclude: tuple = ()
    include: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "rules", make_rules(self.rules))

    def speed_rule_for(self, class_name: str) -> SpeedRule | None:
        return next((rule for rule in self.rules if rule.matches(class_name)), None)
