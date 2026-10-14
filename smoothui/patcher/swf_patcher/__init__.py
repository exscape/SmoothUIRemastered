"""Re-time Scaleform/AS3 timeline animations for a higher frame rate.

Raising a SWF's frame rate (e.g. 30 -> 120) makes every timeline animation play faster. This
raises the frame rate AND compensates, so animations keep their original speed:

  * every animated sprite gets extra empty frames, so each original frame is held for
    R = target fps / source fps frames (with cumulative rounding when R is not an integer: at 22 -> 120
    fps, frames are held for 5, 5, 6, 5, 5, 6, ... frames). Frame labels, nested clips, morph
    ratios etc. are untouched.
  * AS3 frame scripts are index based (addFrameScript(frameIdx, fn)); those constructor calls are
    rewritten (branch / exception offsets are fixed up properly).
  * literal gotoAndPlay(2) / gotoAndStop(3) calls on stretched clips are remapped.
  * persistent ENTER_FRAME handlers of the form `obj.prop += K` (rotation, x, y, alpha, ...) get K / R.
  * tweened objects (consecutive moves of matrix / colour transform / morph ratio) get
    interpolated moves on the extra frames, so they animate at the full frame rate. The original
    frames stay bit-identical. Shape/bitmap swaps, appear/disappear, filters etc. stay stepped.
  * clips that code addresses by a COMPUTED frame number (colour / icon selectors, progress strips)
    and "selector" clips (every frame carries a label or a frame script) are NOT stretched: that would
    change which frame a given number selects. Neither are "still" clips, whose content only
    changes where a stop() halts them first (CLIK state clips, icon sets): how long their frames
    last never shows. The report lists each exclusion and why.

Frame rate independent things (GTween, scaleform.clik.motion.Tween, Timer, setTimeout, getTimer)
need nothing. AS3 only (one DoABC tag); AS2 DoAction clips are not handled.

Modules, roughly in the order the work happens:
  swf_tags       SWF tag lists: parsing and serializing, and the simple tags
  movie          the parsed SWF body: sprites, linkage classes, ActionScript classes
  abc_file       ABC (ActionScript bytecode file) parsing and serializing
  abc_bytecode   method bytecode: disassembly, re-assembly, stack model
  code_analysis  finding frame-number dependent code (gotoAndStop, addFrameScript, ENTER_FRAME, ...)
  planner        deciding which sprites to stretch, and which code constants to change
  interpolation  display list simulation and tween detection
  builder        building the new body
  verify         checking the new body against the original and the plan
  report         the JSON-compatible report, and its text form
"""

from .movie import RescaleError
from .options import FpsRule, SpriteRef, make_rules, parse_sprite_pattern
from .report import format_unmet_rule, report_settings, report_to_text
from .rescale import RescaleResult, rescale_body

__all__ = ["FpsRule", "RescaleError", "RescaleResult", "SpriteRef", "format_unmet_rule", "make_rules",
           "parse_sprite_pattern", "report_settings", "report_to_text", "rescale_body"]
