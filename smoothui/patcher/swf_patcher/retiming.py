"""Mapping original frames to ticks at the new frame rate"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


def round_half_up(value: Fraction) -> int:
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def fps_ratio(source_fps: float, target_fps: float) -> Fraction:
    """target / source as an exact fraction (e.g. 22 -> 120 fps gives 60/11)"""
    return Fraction(target_fps).limit_denominator(1000) / Fraction(source_fps).limit_denominator(1000)


@dataclass(frozen=True)
class Retiming:
    """Holds every original frame for `ratio` ticks, rounding cumulatively: at 60/11, the frames
    are held for 5, 5, 6, 5, 5, 6, ... ticks, so frame k always starts at round(k * ratio)."""
    ratio: Fraction

    def tick(self, frame: int) -> int:
        """The new 0-based frame at which original 0-based frame `frame` starts"""
        return round_half_up(frame * self.ratio)

    def hold(self, frame: int) -> int:
        """Number of new frames that original frame `frame` lasts"""
        return self.tick(frame + 1) - self.tick(frame)

    def frame_number(self, frame: int) -> int:
        """The new 1-based frame number (as used by gotoAndPlay etc.) for a 1-based frame number"""
        return self.tick(frame - 1) + 1

    def hold_lengths(self) -> list[int]:
        """The distinct hold lengths, e.g. [5, 6]"""
        return sorted({self.hold(k) for k in range(200)})
