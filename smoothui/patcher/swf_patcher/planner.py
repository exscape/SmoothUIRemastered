"""Deciding what to change: which sprites to stretch (and to which frame rate), and which constants in
the ActionScript code must change along with them.

A sprite is stretched unless something suggests that doing so would break it. Every reason found is
recorded, and ends up in the report.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from .abc_bytecode import PushDouble
from .code_analysis import (
    ArgKind,
    CodeAnalysis,
    FrameScripts,
    GotoCall,
    Receiver,
    ReceiverKind,
)
from .interpolation import (
    TweenStats,
    TweenStep,
    change_magnitude,
    find_tweens,
    interpolate,
    simulate_timeline,
)
from .movie import SwfMovie
from .options import RescaleOptions
from .place_object import build_move
from .retiming import Retiming, fps_ratio

# Display properties for which a per-frame `obj.prop += K` is an animation that should keep its speed
ANIMATED_PROPERTIES = frozenset({
    "rotation", "x", "y", "alpha", "scaleX", "scaleY", "rotationX", "rotationY", "rotationZ", "width", "height",
})

# Owners of ENTER_FRAME handlers that are known to be harmless at any frame rate
SAFE_HANDLER_PREFIXES = (
    "scaleform.clik.core.UIComponent.",     # CLIK's validation pass: one-shot via validateNow()
)


@dataclass
class IncrementPatch:
    """A per-frame `obj.prop += K` in an ENTER_FRAME handler, scaled down to keep its speed"""
    handler: str
    prop: str
    old: float
    new: float


@dataclass
class SpriteTweens:
    steps: dict[int, list[TweenStep]]   # see interpolation.find_tweens
    stats: TweenStats
    added_bytes: int    # estimated size of the interpolated moves
    motion: float       # total change magnitude: how noticeable the interpolation is


@dataclass
class UnmetRule:
    """An explicit rule that was not followed for a sprite"""
    rule: str           # e.g. "fps rule 'pattern' (80 fps)"
    character_id: int
    problem: str


@dataclass
class RescalePlan:
    source_fps: float
    target_fps: float
    retiming: Retiming                  # for the file as a whole
    # Stretched sprites -> the frame rate they are retimed to, and how
    sprite_fps: dict[int, float] = field(default_factory=dict)
    sprite_retiming: dict[int, Retiming] = field(default_factory=dict)
    # Why each (multi-frame) sprite is not stretched; empty for stretched ones
    reasons: defaultdict = field(default_factory=lambda: defaultdict(list))
    # Not stretched, but probably could be: character id -> its labels
    probably_safe: dict[int, list[str]] = field(default_factory=dict)
    # Classes with frame-number dependent code, that no clip in this file is linked to
    unlinked_classes: set[str] = field(default_factory=set)
    # method index -> {instruction index: new int value or PushDouble}
    code_edits: defaultdict = field(default_factory=lambda: defaultdict(dict))
    new_doubles: list[float] = field(default_factory=list)  # appended to the ABC double pool
    frame_script_edits: int = 0
    goto_edits: int = 0
    increments: list[IncrementPatch] = field(default_factory=list)
    tweens: dict[int, SpriteTweens] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Character ids matched by each of options.rules (in the same order)
    rule_matches: list[list[int]] = field(default_factory=list)
    # Explicit rules (fps rules, include) that could not be followed, see UnmetRule
    unmet_rules: list[UnmetRule] = field(default_factory=list)

    @property
    def stretched(self) -> set[int]:
        return set(self.sprite_retiming)


def short_owner(owner: str) -> str:
    """"pkg.sub.Class.method" -> "Class.method\""""
    return ".".join(owner.split(".")[-2:])


class Planner:
    def __init__(self, movie: SwfMovie, options: RescaleOptions):
        self.movie = movie
        self.options = options
        self.analysis = CodeAnalysis(movie)
        self.plan = RescalePlan(movie.fps, options.target_fps,
                                Retiming(fps_ratio(movie.fps, options.target_fps)))
        self.reasons = self.plan.reasons

    def make_plan(self) -> RescalePlan:
        frame_scripts = self.analysis.find_frame_scripts()
        literal_gotos, unresolved_gotos = [], []

        self._check_frame_scripts(frame_scripts)
        self._check_selectors(frame_scripts)
        self._check_goto_calls(literal_gotos, unresolved_gotos)
        frame_property_uses = self._check_frame_property_uses()
        self._apply_include_exclude()
        self._find_probably_safe()
        self._apply_fps_rules()

        self._choose_stretched(frame_scripts, literal_gotos)
        self._check_explicit_rules()
        self._patch_frame_scripts(frame_scripts)
        self._patch_literal_gotos(literal_gotos)
        self._patch_frame_handlers()
        self._warn_unresolved(unresolved_gotos, frame_property_uses)
        self._plan_interpolation()
        return self.plan

    def _target_sprites(self, class_name: str | None, receiver: Receiver) -> set[int] | None:
        """The sprites code in class_name can be addressing via receiver; None if unknown"""
        if receiver.kind == ReceiverKind.UNRESOLVED or not class_name:
            return None
        linked = self.movie.sprites_linked_to(class_name)
        if not linked:
            self.plan.unlinked_classes.add(class_name)
            return set()
        if receiver.kind == ReceiverKind.THIS:
            return linked
        by_name = self.movie.descendants_by_instance_name(class_name)
        return set().union(*(by_name.get(name, set()) for name in receiver.names))

    # --- Reasons not to stretch ---

    def _check_frame_scripts(self, frame_scripts: dict[str, FrameScripts]):
        for class_name, scripts in frame_scripts.items():
            if not scripts.recognised:
                for character_id in self.movie.sprites_of_class.get(class_name, []):
                    self.reasons[character_id].append(
                        "addFrameScript constructor has an unrecognised shape (cannot re-index)")

    def _check_selectors(self, frame_scripts: dict[str, FrameScripts]):
        """Clips where every frame has a label or a script are "selectors" (icon sets and the like),
        addressed by frame rather than played"""
        script_frames = defaultdict(set)
        for class_name, scripts in frame_scripts.items():
            for character_id in self.movie.sprites_of_class.get(class_name, []):
                for call in scripts.calls:
                    script_frames[character_id].update(frame for _, frame in call)

        for sprite in self.movie.multi_frame_sprites():
            marked = set(sprite.labels) | script_frames[sprite.character_id]
            if all(frame in marked for frame in range(sprite.frame_count)):
                self.reasons[sprite.character_id].append("selector clip: every frame has a label or frame script")
            if sprite.frame_count != sprite.declared_frame_count:
                self.reasons[sprite.character_id].append("declared frame count != ShowFrame count")

    def _check_goto_calls(self, literal_gotos: list, unresolved_gotos: list):
        """Clips that code addresses by a computed frame number can't be stretched. Literal frame
        numbers can be rewritten, if the clips they may refer to are known."""
        for call in self.analysis.find_goto_calls():
            if call.arg.kind == ArgKind.LABEL:
                continue
            targets = self._target_sprites(call.class_name, call.receiver)
            if targets is None:
                if call.arg.kind == ArgKind.COMPUTED or call.arg.value > 1:
                    unresolved_gotos.append(call)
                continue
            if call.arg.kind == ArgKind.COMPUTED:
                for character_id in targets:
                    self.reasons[character_id].append(f"computed {call.call}() in {short_owner(call.owner)}")
            elif call.arg.value > 1:
                literal_gotos.append((call, targets))

    def _check_frame_property_uses(self):
        """totalFrames, currentFrame etc. change meaning when a clip is stretched"""
        uses = []
        for use in self.analysis.find_frame_property_uses():
            targets = self._target_sprites(use.class_name, use.receiver)
            uses.append((use, targets))
            for character_id in targets or ():
                self.reasons[character_id].append(f"{use.prop} read in {short_owner(use.owner)}")
        return uses

    def _apply_include_exclude(self):
        for character_id in self.movie.sprites:
            if self._matches(self.options.exclude, character_id):
                self.reasons[character_id].append("excluded by the exclude list")
        for character_id in self.movie.sprites:
            if self._matches(self.options.include, character_id):
                # Overrides everything except an explicit exclude, and code that can't be patched
                self.reasons[character_id] = [reason for reason in self.reasons[character_id]
                                              if reason.startswith("excluded by") or "unrecognised" in reason]

    def _matches(self, tokens, character_id) -> bool:
        class_name = self.movie.class_name(character_id)
        return any(int(token) == character_id if token.isdigit() else re.search(token, class_name)
                   for token in tokens)

    def _find_probably_safe(self):
        """Clips excluded only because an untyped value is passed to gotoAndStop/Play, yet whose
        timeline has label segments (rather than one label per frame): addressing those by number
        would need magic numbers, so the value is almost certainly a label"""
        for sprite in self.movie.multi_frame_sprites():
            reasons = self.reasons.get(sprite.character_id, [])
            if reasons and sprite.labels and all(reason.startswith("computed ") for reason in reasons):
                self.plan.probably_safe[sprite.character_id] = sorted(sprite.labels.values())
                if self.options.include_likely:
                    self.reasons[sprite.character_id] = []

    def _apply_fps_rules(self):
        """A rule at the file's original frame rate is allowed: the sprite is then "stretched" by a
        factor of 1, i.e. left as it is, and plays target/original times as fast. Below that would
        need frames to be dropped."""
        plan = self.plan
        for sprite in self.movie.multi_frame_sprites():
            rule = self.options.fps_rule_for(self.movie.class_name(sprite.character_id))
            if rule and fps_ratio(plan.source_fps, rule.fps) < 1:
                self.reasons[sprite.character_id].append(
                    f"fps rule '{rule.pattern.pattern}' asks for {rule.fps:g} fps, below the original "
                    f"{plan.source_fps:g} fps (that would need dropping frames, which is not supported)")
            plan.sprite_fps[sprite.character_id] = rule.fps if rule else plan.target_fps

    # --- Choosing the stretched set ---

    def _choose_stretched(self, frame_scripts, literal_gotos):
        """Start from every clip without a reason not to stretch it. Then, until nothing changes:
        clips sharing a frame-script class must all be stretched to the same frame rate, as must all
        the clips that a literal gotoAndPlay(N) may refer to. Otherwise none of them are."""
        plan = self.plan
        stretched = {sprite.character_id for sprite in self.movie.multi_frame_sprites()
                     if not self.reasons[sprite.character_id]}

        def unstretch(ids, reason):
            for character_id in ids:
                stretched.discard(character_id)
                self.reasons[character_id].append(reason)

        def unstretch_for_fps_conflict(ids, reason):
            """Clips without a rule of their own are collateral damage of the rules involved; report
            that against those rules (clips with a rule are reported by _check_explicit_rules)"""
            unstretch(ids, reason)
            rules = {self.options.fps_rule_for(self.movie.class_name(c)) for c in ids} - {None}
            for character_id in ids:
                if self.options.fps_rule_for(self.movie.class_name(character_id)) is None:
                    for rule in rules:
                        self._unmet(rule.description, character_id,
                                    f"default-rate clip not stretched because of this rule: {reason}")

        changed = True
        while changed:
            changed = False
            for class_name, ids in self.movie.sprites_of_class.items():
                if class_name not in frame_scripts or len(ids) < 2:
                    continue
                inside = [c for c in ids if c in stretched]
                if inside and len(inside) != len(ids):
                    unstretch(inside, f"shares class {class_name} with a clip that is not stretched")
                    changed = True
                elif inside and len({plan.sprite_fps[c] for c in inside}) > 1:
                    unstretch_for_fps_conflict(inside, f"shares class {class_name} with a clip retimed to a different frame rate")
                    changed = True
            for call, targets in literal_gotos:
                inside = targets & stretched
                if inside and inside != targets:
                    unstretch(inside, f"literal {call.call}({call.arg.value}) in {short_owner(call.owner)} "
                                      "may also target clips that are not stretched")
                    changed = True
                elif inside and len({plan.sprite_fps[c] for c in inside}) > 1:
                    unstretch_for_fps_conflict(inside, f"literal {call.call}({call.arg.value}) in {short_owner(call.owner)} "
                                      "targets clips retimed to different frame rates")
                    changed = True

        retimings = {}  # one per distinct frame rate
        for character_id in sorted(stretched):
            fps = plan.sprite_fps[character_id]
            if fps not in retimings:
                retimings[fps] = Retiming(fps_ratio(plan.source_fps, fps))
            plan.sprite_retiming[character_id] = retimings[fps]
        plan.sprite_fps = {c: fps for c, fps in plan.sprite_fps.items() if c in stretched}

    def _unmet(self, rule: str, character_id: int, problem: str):
        self.plan.unmet_rules.append(UnmetRule(rule, character_id, problem))

    def _check_explicit_rules(self):
        """Every sprite an fps rule or include matches must end up as the rule says"""
        plan = self.plan
        plan.rule_matches = [[] for _ in self.options.rules]
        for character_id, sprite in sorted(self.movie.sprites.items()):
            class_name = self.movie.class_name(character_id)
            matching = [i for i, rule in enumerate(self.options.rules) if rule.matches(class_name)]
            for i in matching:
                plan.rule_matches[i].append(character_id)
            if matching:
                self._check_fps_rule(sprite, [self.options.rules[i] for i in matching])

            include = next((token for token in self.options.include if self._matches([token], character_id)), None)
            if include is not None and sprite.frame_count > 1 and character_id not in plan.sprite_retiming:
                self._unmet(f"include '{include}'", character_id,
                            "not stretched: " + "; ".join(dict.fromkeys(self.reasons[character_id])))

    def _check_fps_rule(self, sprite, matching_rules):
        plan = self.plan
        cid = sprite.character_id
        rule, *shadowed = matching_rules
        for other in shadowed:
            if other.fps != rule.fps:
                self._unmet(other.description, cid, f"overridden by the earlier rule {rule.description}")

        if sprite.frame_count <= 1:
            self._unmet(rule.description, cid, "the sprite has a single frame, so there is nothing to retime "
                                               "(rules don't apply to the clips inside it)")
        elif cid in plan.sprite_retiming:
            if plan.sprite_fps[cid] != rule.fps:    # can't happen; checked to be sure
                self._unmet(rule.description, cid, f"stretched to {plan.sprite_fps[cid]:g} fps instead")
        elif fps_ratio(plan.source_fps, rule.fps) != 1:
            # (At the original rate, leaving the sprite as it is *is* what the rule asks for)
            self._unmet(rule.description, cid,
                        "not stretched: " + "; ".join(dict.fromkeys(self.reasons[cid])))

    # --- Code changes ---

    def _patch_frame_scripts(self, frame_scripts):
        """addFrameScript(frame, ...) frame indices of stretched classes"""
        plan = self.plan
        for class_name, scripts in frame_scripts.items():
            ids = self.movie.sprites_of_class.get(class_name, [])
            if not (ids and scripts.recognised and all(c in plan.sprite_retiming for c in ids)):
                continue
            retiming = plan.sprite_retiming[ids[0]]
            if retiming.ratio == 1:
                continue
            for call in scripts.calls:
                for push_index, frame in call:
                    plan.code_edits[scripts.method][push_index] = retiming.tick(frame)
                    plan.frame_script_edits += 1

    def _patch_literal_gotos(self, literal_gotos: list[tuple[GotoCall, set[int]]]):
        plan = self.plan
        for call, targets in literal_gotos:
            if targets and targets <= plan.stretched:
                retiming = plan.sprite_retiming[next(iter(targets))]
                if retiming.ratio == 1:
                    continue
                plan.code_edits[call.method][call.push_index] = retiming.frame_number(call.arg.value)
                plan.goto_edits += 1

    def _patch_frame_handlers(self):
        """ENTER_FRAME handlers run once per frame, so they now run more often. Scale per-frame
        increments of animated properties to compensate; warn about the rest."""
        plan = self.plan
        ratio = plan.retiming.ratio
        double_pool_size = len(self.movie.abc.doubles)
        for handler in self.analysis.find_frame_handlers():
            if handler.one_shot or handler.time_based or handler.owner.startswith(SAFE_HANDLER_PREFIXES):
                continue
            name = short_owner(handler.owner)
            patched = False
            if self.options.scale_handlers:
                for push_index, step, prop in handler.increments:
                    if prop not in ANIMATED_PROPERTIES:
                        continue
                    new_step = step / float(ratio) if isinstance(step, float) \
                        else float(Fraction(step).limit_denominator(10**6) / ratio)
                    plan.code_edits[handler.method][push_index] = PushDouble(double_pool_size + len(plan.new_doubles))
                    plan.new_doubles.append(new_step)
                    plan.increments.append(IncrementPatch(name, prop, float(step), new_step))
                    patched = True
            if not patched:
                plan.warnings.append(f"persistent ENTER_FRAME handler {name} runs once per frame "
                                     f"(now {float(ratio):.2f}x as often); check it for frame counting")

    def _warn_unresolved(self, unresolved_gotos, frame_property_uses):
        plan = self.plan
        for call in unresolved_gotos:
            if not call.class_name or not self.movie.sprites_linked_to(call.class_name):
                continue    # class not instantiated by any clip in this file
            if call.arg.kind == ArgKind.NUMBER:
                description = f"literal {call.call}({call.arg.value})"
            else:
                description = f"computed {call.call}(...)"
            warning = (f"unresolved receiver: {description} in {call.owner} -- "
                       "if it targets a stretched clip, check by hand")
            if warning not in plan.warnings:
                plan.warnings.append(warning)

        for use, targets in frame_property_uses:
            if targets is None:
                plan.warnings.append(f"{use.prop} used in {use.owner} on an unresolved receiver")
            elif targets & plan.stretched:
                plan.warnings.append(f"{use.prop} used in {use.owner} on stretched clips {sorted(targets & plan.stretched)}")

    # --- Interpolation ---

    def _plan_interpolation(self):
        plan = self.plan
        for character_id, retiming in sorted(plan.sprite_retiming.items()):
            if retiming.ratio == 1:
                continue    # no frames in between to interpolate
            steps, stats = find_tweens(simulate_timeline(self.movie.sprites[character_id].tags),
                                       self.options.min_motion)
            if not steps:
                continue
            added_bytes, motion = 0, 0.0
            for frame, frame_steps in steps.items():
                extra_ticks = retiming.hold(frame) - 1
                for step in frame_steps:
                    move = build_move(step.depth, *interpolate(step.start, step.end, 0.5))
                    added_bytes += extra_ticks * len(move.to_bytes())
                    motion += change_magnitude(step.start, step.end)
            plan.tweens[character_id] = SpriteTweens(steps, stats, added_bytes, motion)
