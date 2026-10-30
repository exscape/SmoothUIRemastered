"""The entry point: rescale one decompressed SWF body to a higher frame rate"""

from __future__ import annotations

from dataclasses import dataclass

from .builder import build_body
from .movie import RescaleError, SwfMovie
from .options import RescaleOptions
from .planner import Planner
from .report import build_report, unchanged_report
from .verify import verify

MAX_PROBLEMS_SHOWN = 10


@dataclass(slots=True)
class RescaleResult:
    body: bytes | None      # the new body; None if the file was left unchanged
    report: dict            # see report.py; report_to_text() makes it readable

    @property
    def old_fps(self) -> float:
        return self.report["old_fps"]

    @property
    def new_fps(self) -> float | None:
        """None if the file was left unchanged"""
        return self.report["new_fps"]

    @property
    def unmet_rules(self) -> list[dict]:
        """Explicit rules that were not followed (see report.py); empty if all went as asked"""
        return self.report["unmet_rules"]


def rescale_body(body: bytes, target_fps: float, *, rules=(), include=(), exclude=(),
                 shares_classes=True, accept_plan=None) -> RescaleResult | None:
    """Raise the frame rate of an SWF to target_fps, retiming its animations to keep their speed.

    body is the decompressed SWF after the 8-byte signature/version/length header. The new body
    usually has a different length, so the header's length field must be updated to match.

    rules is a sequence of SpeedRules or (class name regex, speed) pairs, to play matching sprites
    faster or slower than their original speed; see SpeedRule. For the other arguments, see
    RescaleOptions. accept_plan(movie, plan), if given, sees the plan before anything is built; if it
    returns False, nothing is built and None is returned.

    Files already at or above target_fps are left unchanged (new_fps and body are None).
    Rules that can't be followed (e.g. a sprite matching a SpeedRule is not safe to stretch) don't
    raise; they are listed in result.unmet_rules, which callers must check.
    Raises RescaleError if the file is not supported, or if the result fails verification.
    """
    options = RescaleOptions(target_fps, tuple(rules), tuple(exclude), tuple(include), shares_classes)
    movie = SwfMovie(body)
    if target_fps <= movie.fps:
        return RescaleResult(None, unchanged_report(movie.fps, options))

    plan = Planner(movie, options).make_plan()
    if accept_plan is not None and not accept_plan(movie, plan):
        return None
    new_body = build_body(movie, plan)
    problems = verify(movie, plan, new_body)
    if problems:
        raise RescaleError("verification failed: " + "; ".join(problems[:MAX_PROBLEMS_SHOWN]))
    return RescaleResult(new_body, build_report(movie, options, plan))

