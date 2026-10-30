"""Deciding what to change: which sprites to stretch (and at which speed), and which constants in
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
    MOTION_PACKAGE,
    ArgKind,
    FrameScripts,
    GotoCall,
    MovieAnalysis,
    Receiver,
    ReceiverKind,
)
from .interpolation import (
    TweenSample,
    TweenStats,
    TweenStep,
    change_magnitude,
    find_tweens,
    sample_tweens,
    simulate_timeline,
    tween_moves,
)
from .movie import SwfMovie
from .options import RescaleOptions, SpriteRef, format_speed
from .retiming import Retiming, fps_ratio
from .swf_tags import Tag

# Display properties for which a per-frame `obj.prop += K` is an animation that should keep its speed
ANIMATED_PROPERTIES = frozenset({
    "rotation", "x", "y", "alpha", "scaleX", "scaleY", "rotationX", "rotationY", "rotationZ", "width", "height",
})

# Owners of ENTER_FRAME handlers that are known to be harmless at any frame rate
SAFE_HANDLER_PREFIXES = (
    "scaleform.clik.core.UIComponent.",     # CLIK's validation pass: one-shot via validateNow()
)


@dataclass(slots=True)
class IncrementPatch:
    """A per-frame `obj.prop += K` in an ENTER_FRAME handler, scaled down to keep its speed"""
    handler: str
    prop: str
    old: float
    new: float


@dataclass(slots=True)
class SpriteTweens:
    steps: dict[int, list[TweenStep]]       # see interpolation.find_tweens
    samples: dict[int, list[TweenSample]]   # see interpolation.sample_tweens
    moves: dict[int, list[Tag]]             # see interpolation.tween_moves
    stats: TweenStats
    added_bytes: int    # size of the interpolated moves
    motion: float       # total change magnitude: how noticeable the interpolation is


@dataclass(frozen=True, slots=True)
class PlanWarning:
    """Something the patcher can't check, which may break a stretched clip"""
    key: str        # kind and code location, stable between builds, e.g. "unresolved-goto Class.method"
    message: str
    code_hash: str  # of the code at that location (see CodeAnalysis.code_hash), so reviews can be tied to it


@dataclass(slots=True)
class UnmetRule:
    """An explicit rule that was not followed for a sprite"""
    rule: str           # as written in a profile, e.g. "speed 1.5: pattern" or "include: sprite 338"
    character_id: int
    problem: str


@dataclass(slots=True)
class RescalePlan:
    source_fps: float
    target_fps: float
    retiming: Retiming                  # for the file as a whole
    # Stretched sprites -> their speed (1 = their original speed; see SpeedRule), and how they are retimed
    sprite_speed: dict[int, Fraction] = field(default_factory=dict)
    sprite_retiming: dict[int, Retiming] = field(default_factory=dict)
    # Why each (multi-frame) sprite is not stretched; empty for stretched ones
    reasons: defaultdict = field(default_factory=lambda: defaultdict(list))
    # Not stretched, but probably could be: character id -> its labels
    probably_safe: dict[int, list[str]] = field(default_factory=dict)
    # Classes with frame-number dependent code, that no clip in this file is linked to
    unlinked_classes: set[str] = field(default_factory=set)
    # method (the movie's id, see AbcUnit) -> {instruction index: new int value or PushDouble}
    code_edits: defaultdict = field(default_factory=lambda: defaultdict(dict))
    # DoABC tag index -> the values appended to that tag's double pool
    new_doubles: dict[int, list[float]] = field(default_factory=dict)
    frame_script_edits: int = 0
    goto_edits: int = 0
    increments: list[IncrementPatch] = field(default_factory=list)
    tweens: dict[int, SpriteTweens] = field(default_factory=dict)
    warnings: list[PlanWarning] = field(default_factory=list)
    # Character ids matched by each of options.rules / include / exclude (in the same order)
    rule_matches: list[list[int]] = field(default_factory=list)
    include_matches: list[list[int]] = field(default_factory=list)
    exclude_matches: list[list[int]] = field(default_factory=list)
    # Explicit rules (speed rules, include, exclude) that could not be followed, see UnmetRule
    unmet_rules: list[UnmetRule] = field(default_factory=list)

    @property
    def stretched(self) -> set[int]:
        return set(self.sprite_retiming)

    @property
    def max_speed(self) -> Fraction:
        """The highest speed a sprite can get without dropping frames: target / source fps, at
        which its frames are not repeated"""
        return self.retiming.ratio


def _approximate(value: Fraction) -> str:
    """A speed for a message: exact where that is short (2, 1.5), otherwise rounded (5.45)"""
    text = format_speed(value)
    return text if "/" not in text else f"{float(value):.2f}"


def short_owner(owner: str) -> str:
    """"pkg.sub.Class.method" -> "Class.method\""""
    return ".".join(owner.split(".")[-2:])


def _describe_goto(call: GotoCall, unknown="...") -> str:
    """"literal gotoAndStop(5)", "gotoAndStop(2 or 1)" (a choice between literals) or "computed gotoAndStop(...)\""""
    if call.arg.kind == ArgKind.NUMBER:
        return f"literal {call.call}({call.arg.value})"
    if call.arg.choices:
        return f"{call.call}({' or '.join(map(str, call.arg.choices))})"
    return f"computed {call.call}({unknown})"


def _comes_to_rest(frame_count, start, changes, stops, scripted) -> bool:
    """Does playback from frame start reach a stop() before anything else changes?"""
    for step in range(frame_count):
        frame = (start + step) % frame_count
        if step and frame in changes:
            return False
        if frame in stops:
            return True
    return start not in scripted    # loops back to start, which looks the same (but runs its script again)


class Planner:
    def __init__(self, movie: SwfMovie, options: RescaleOptions):
        self.movie = movie
        self.options = options
        self.analysis = MovieAnalysis(movie, options.shares_classes)
        self.plan = RescalePlan(movie.fps, options.target_fps,
                                Retiming(fps_ratio(movie.fps, options.target_fps)))
        self.reasons = self.plan.reasons
        self.mismatched_refs = set()    # SpriteRefs whose sprite doesn't match what they expect

    def make_plan(self) -> RescalePlan:
        frame_scripts = self.analysis.find_frame_scripts()
        literal_gotos, unresolved_gotos = [], []

        self._check_frame_scripts(frame_scripts)
        self._check_selectors(frame_scripts)
        self._check_goto_calls(literal_gotos, unresolved_gotos)
        self._check_still_clips(frame_scripts, literal_gotos, unresolved_gotos)
        frame_property_uses = self._check_frame_property_uses()
        self._check_sprite_refs()
        self._apply_include_exclude()
        self._find_probably_safe()
        self._apply_speed_rules()

        self._choose_stretched(frame_scripts, literal_gotos)
        self._check_explicit_rules()
        self._patch_frame_scripts(frame_scripts)
        self._patch_literal_gotos(literal_gotos)
        self._patch_frame_handlers()
        self._warn_frame_rate_writes()
        self._warn_unresolved(unresolved_gotos, frame_property_uses)
        self._plan_interpolation(frame_scripts, literal_gotos)
        return self.plan

    def _target_sprites(self, method: int, receiver: Receiver) -> set[int] | None:
        """The sprites code in method can be addressing via receiver; None if unknown"""
        class_name = self.movie.class_of_method.get(method)
        if receiver.kind == ReceiverKind.UNRESOLVED or not class_name:
            return None
        linked = self.movie.sprites_linked_to(class_name)
        created = set().union(*(self.movie.sprites_linked_to(c) for c in receiver.created or ()))
        if not linked:
            if receiver.kind == ReceiverKind.NAMED and self.movie.may_run(method):
                # Created by code, so its children can be anything added to it; a field can only
                # hold what code stores in it, where that is known
                return created if receiver.created is not None else None
            self.plan.unlinked_classes.add(class_name)
            return set()
        if receiver.kind == ReceiverKind.THIS:
            return linked
        by_name = self.movie.descendants_by_instance_name(class_name)
        return created.union(*(by_name.get(name, set()) for name in receiver.names))

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
                script_frames[character_id].update(scripts.frames)

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
            targets = self._target_sprites(call.method, call.receiver)
            if targets is None:
                if call.arg.kind == ArgKind.COMPUTED or call.arg.value > 1:
                    unresolved_gotos.append(call)
                continue
            if call.arg.kind == ArgKind.COMPUTED:
                for character_id in targets:
                    self.reasons[character_id].append(f"{_describe_goto(call, '')} in {short_owner(call.owner)}")
            elif call.arg.value > 1:
                literal_gotos.append((call, targets))

    def _check_still_clips(self, frame_scripts, literal_gotos, unresolved_gotos):
        """Clips that come to rest wherever their content changes: wherever playback starts, a
        stop() comes before anything else changes, so how long each frame lasts never shows, and
        stretching them changes nothing. Mostly CLIK state clips (each state starts at a label and
        ends with a stop()) and icon sets. Playback starts at frame 1, at labels and at changes (as
        gotoAndPlay(label) and stops leave it), or anywhere for clips that play() or gotoAndPlay(N)
        may be used on."""
        played_anywhere = self._played_anywhere(literal_gotos, unresolved_gotos)
        for sprite in self.movie.multi_frame_sprites():
            scripts = frame_scripts.get(self.movie.class_name(sprite.character_id))
            if scripts and not scripts.recognised:
                continue
            stops = scripts.stop_frames if scripts else set()
            scripted = (scripts.frames if scripts else set()) - stops    # scripts that do more than stop()
            changes = sprite.changed_frames() | scripted
            if played_anywhere is None or sprite.character_id in played_anywhere:
                starts = range(sprite.frame_count)
            else:
                starts = {0, *sprite.labels, *changes}
            if all(_comes_to_rest(sprite.frame_count, start, changes, stops, scripted) for start in starts):
                self.reasons[sprite.character_id].append("still clip: it stops before each change, so frame timing never shows")

    def _played_anywhere(self, literal_gotos, unresolved_gotos) -> set[int] | None:
        """The clips that play() or gotoAndPlay(N) may start playing at any frame; None if that may
        be any clip"""
        if any(call.call == "gotoAndPlay" and self.movie.may_run(call.method) for call in unresolved_gotos):
            return None
        played = set().union(*(targets for call, targets in literal_gotos if call.call == "gotoAndPlay"))
        for method, receiver in self.analysis.find_play_calls():
            if self.movie.may_run(method):
                targets = self._target_sprites(method, receiver)
                if targets is None:
                    return None
                played |= targets
        return played

    def _check_frame_property_uses(self):
        """totalFrames, currentFrame etc. change meaning when a clip is stretched"""
        uses = []
        motion_hosts = self.analysis.find_static_motion_hosts()
        for use in self.analysis.find_frame_property_uses():
            targets = self._target_sprites(use.method, use.receiver)
            if targets is None and motion_hosts is not None and use.owner.startswith(MOTION_PACKAGE):
                # The motion library follows the timeline of the clips hosting the animations. As
                # every motion lasts one frame, the frame numbers make no difference; to be sure,
                # those clips are left unstretched (they usually have a single frame anyway).
                targets = set().union(*(self.movie.sprites_linked_to(host) for host in motion_hosts))
            uses.append((use, targets))
            for character_id in targets or ():
                self.reasons[character_id].append(f"{use.prop} read in {short_owner(use.owner)}")
        return uses

    def _check_sprite_refs(self):
        """SpriteRefs with an expected class and frame count that the sprite doesn't match are not applied"""
        describe = lambda class_and_frames: f"{class_and_frames[0] or '(no class)'} with {class_and_frames[1]} frames"
        for kind, tokens in (("include", self.options.include), ("exclude", self.options.exclude)):
            for ref in tokens:
                if not isinstance(ref, SpriteRef) or ref.expected is None:
                    continue
                sprite = self.movie.sprites.get(ref.id)
                found = (self.movie.class_of_sprite.get(ref.id), sprite.frame_count) if sprite else None
                if found != ref.expected:
                    self.mismatched_refs.add(ref)
                    self._unmet(f"{kind}: {ref}", ref.id,
                                f"expected {describe(ref.expected)}, found "
                                f"{describe(found) if found else 'no such sprite'}; not applied")

    def _apply_include_exclude(self):
        plan = self.plan
        plan.exclude_matches = [self._matching_sprites(token) for token in self.options.exclude]
        plan.include_matches = [self._matching_sprites(token) for token in self.options.include]
        for character_id in set().union(*plan.exclude_matches):
            self.reasons[character_id].append("excluded by the exclude list")
        for character_id in set().union(*plan.include_matches):
            # Overrides everything except an explicit exclude, and code that can't be patched
            self.reasons[character_id] = [reason for reason in self.reasons[character_id]
                                          if reason.startswith("excluded by") or "unrecognised" in reason]

    def _matching_sprites(self, token) -> list[int]:
        """The ids of the sprites an include/exclude token (a SpriteRef or class name regex) matches"""
        if isinstance(token, SpriteRef):
            return [token.id] if token.id in self.movie.sprites and token not in self.mismatched_refs else []
        return [character_id for character_id in sorted(self.movie.sprites)
                if (class_name := self.movie.class_name(character_id)) and re.search(token, class_name)]

    def _find_probably_safe(self):
        """Clips excluded only because an untyped value is passed to gotoAndStop/Play, yet whose
        timeline has label segments (rather than one label per frame): addressing those by number
        would need magic numbers, so the value is almost certainly a label"""
        for sprite in self.movie.multi_frame_sprites():
            reasons = self.reasons.get(sprite.character_id, [])
            if reasons and sprite.labels and all(reason.startswith("computed ") for reason in reasons):
                self.plan.probably_safe[sprite.character_id] = sorted(sprite.labels.values())

    def _apply_speed_rules(self):
        """A rule at the highest speed (see RescalePlan.max_speed) is allowed: the sprite is then
        "stretched" by a factor of 1, i.e. left as it is. Above that would need frames to be dropped."""
        plan = self.plan
        for sprite in self.movie.multi_frame_sprites():
            rule = self.options.speed_rule_for(self.movie.class_name(sprite.character_id))
            if rule and rule.speed > plan.max_speed:
                self.reasons[sprite.character_id].append(
                    f"'{rule.description}' asks for {format_speed(rule.speed)}x speed, but at most "
                    f"{_approximate(plan.max_speed)}x is possible ({plan.source_fps:g} -> {plan.target_fps:g} fps) "
                    "without dropping frames, which is not supported")
            plan.sprite_speed[sprite.character_id] = rule.speed if rule else Fraction(1)

    # --- Choosing the stretched set ---

    def _choose_stretched(self, frame_scripts, literal_gotos):
        """Start from every clip without a reason not to stretch it. Then, until nothing changes:
        clips sharing a frame-script class must all be stretched to the same speed, as must all the
        clips that a literal gotoAndPlay(N) may refer to. Otherwise none of them are."""
        plan = self.plan
        stretched = {sprite.character_id for sprite in self.movie.multi_frame_sprites()
                     if not self.reasons[sprite.character_id]}

        def unstretch(ids, reason):
            for character_id in ids:
                stretched.discard(character_id)
                self.reasons[character_id].append(reason)

        def unstretch_for_speed_conflict(ids, reason):
            """Clips without a rule of their own are collateral damage of the rules involved; report
            that against those rules (clips with a rule are reported by _check_explicit_rules)"""
            unstretch(ids, reason)
            rules = {self.options.speed_rule_for(self.movie.class_name(c)) for c in ids} - {None}
            for character_id in ids:
                if self.options.speed_rule_for(self.movie.class_name(character_id)) is None:
                    for rule in rules:
                        self._unmet(rule.description, character_id,
                                    f"normal-speed clip not stretched because of this rule: {reason}")

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
                elif inside and len({plan.sprite_speed[c] for c in inside}) > 1:
                    unstretch_for_speed_conflict(inside, f"shares class {class_name} with a clip played at a different speed")
                    changed = True
            for call, targets in literal_gotos:
                inside = targets & stretched
                if inside and inside != targets:
                    unstretch(inside, f"literal {call.call}({call.arg.value}) in {short_owner(call.owner)} "
                                      "may also target clips that are not stretched")
                    changed = True
                elif inside and len({plan.sprite_speed[c] for c in inside}) > 1:
                    unstretch_for_speed_conflict(inside, f"literal {call.call}({call.arg.value}) in {short_owner(call.owner)} "
                                                 "targets clips played at different speeds")
                    changed = True

        retimings = {}  # one per distinct speed
        for character_id in sorted(stretched):
            speed = plan.sprite_speed[character_id]
            if speed not in retimings:
                retimings[speed] = Retiming(plan.retiming.ratio / speed)
            plan.sprite_retiming[character_id] = retimings[speed]
        plan.sprite_speed = {c: speed for c, speed in plan.sprite_speed.items() if c in stretched}

    def _unmet(self, rule: str, character_id: int, problem: str):
        self.plan.unmet_rules.append(UnmetRule(rule, character_id, problem))

    def _check_explicit_rules(self):
        """Every sprite a speed rule or include matches must end up as the rule says"""
        plan = self.plan
        plan.rule_matches = [[] for _ in self.options.rules]
        for character_id, sprite in sorted(self.movie.sprites.items()):
            class_name = self.movie.class_name(character_id)
            matching = [i for i, rule in enumerate(self.options.rules) if rule.matches(class_name)]
            for i in matching:
                plan.rule_matches[i].append(character_id)
            if matching:
                self._check_speed_rule(sprite, [self.options.rules[i] for i in matching])

        included = {}   # character id -> the first include token that matches it
        for token, ids in zip(self.options.include, plan.include_matches):
            for character_id in ids:
                included.setdefault(character_id, token)
        for character_id, token in sorted(included.items()):
            if self.movie.sprites[character_id].frame_count > 1 and character_id not in plan.sprite_retiming:
                self._unmet(f"include: {token}", character_id,
                            "not stretched: " + "; ".join(dict.fromkeys(self.reasons[character_id])))

    def _check_speed_rule(self, sprite, matching_rules):
        plan = self.plan
        cid = sprite.character_id
        rule, *shadowed = matching_rules
        for other in shadowed:
            if other.speed != rule.speed:
                self._unmet(other.description, cid, f"overridden by the earlier rule {rule.description}")

        if sprite.frame_count <= 1:
            self._unmet(rule.description, cid, "the sprite has a single frame, so there is nothing to retime "
                                               "(rules don't apply to the clips inside it)")
        elif cid in plan.sprite_retiming:
            if plan.sprite_speed[cid] != rule.speed:    # can't happen; checked to be sure
                self._unmet(rule.description, cid, f"stretched for {format_speed(plan.sprite_speed[cid])}x speed instead")
        elif rule.speed != plan.max_speed:
            # (At the highest speed, leaving the sprite as it is *is* what the rule asks for)
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
        for handler in self.analysis.find_frame_handlers():
            if handler.one_shot or handler.time_based or handler.owner.startswith(SAFE_HANDLER_PREFIXES):
                continue
            name = short_owner(handler.owner)
            unit = self.movie.unit_of(handler.method)
            new_doubles = plan.new_doubles.setdefault(unit.tag_index, [])
            patched = False
            for push_index, step, prop in handler.increments:
                if prop not in ANIMATED_PROPERTIES:
                    continue
                new_step = step / float(ratio) if isinstance(step, float) \
                    else float(Fraction(step).limit_denominator(10**6) / ratio)
                plan.code_edits[handler.method][push_index] = PushDouble(len(unit.abc.doubles) + len(new_doubles))
                new_doubles.append(new_step)
                plan.increments.append(IncrementPatch(name, prop, float(step), new_step))
                patched = True
            if not patched:
                self._warn(f"enter-frame-handler {name}",
                           f"persistent ENTER_FRAME handler {name} runs once per frame "
                           f"(now {float(ratio):.2f}x as often); check it for frame counting", [handler.method])

    def _warn_frame_rate_writes(self):
        """Code that sets the movie's frame rate (stage.frameRate) makes it play at that rate, not at the one
        its timelines were retimed for"""
        for method in self.analysis.find_frame_rate_writes():
            name = short_owner(self.movie.owner_name(method))
            self._warn(f"sets-frame-rate {name}",
                       f"{name} sets the screen's frame rate (stage.frameRate) itself, so its animations "
                       "would play at the wrong speed", [method])

    def _warn(self, key: str, message: str, methods):
        """methods: the code the warning is about"""
        warning = PlanWarning(key, message, self.analysis.code_hash(methods))
        if warning not in self.plan.warnings:
            self.plan.warnings.append(warning)

    def _warn_unresolved(self, unresolved_gotos, frame_property_uses):
        """One warning per key (kind and code location), naming everything found there"""
        stretched = self.plan.stretched
        # key -> (kind, {owner: None}, {what: None}, stretched clips, methods); dicts keep the order found
        found = {}

        def add(kind, method, owner, what, clips=()):
            entry = found.setdefault(f"{kind} {short_owner(owner)}", (kind, {}, {}, set(), set()))
            entry[1][owner] = entry[2][what] = None
            entry[3].update(clips)
            entry[4].add(method)

        for call in unresolved_gotos:
            if not self.movie.may_run(call.method):
                continue    # nothing in this file can call it
            add("unresolved-goto", call.method, call.owner, _describe_goto(call))

        for use, targets in frame_property_uses:
            if targets is None and self.movie.may_run(use.method):
                add("unresolved-frame-property", use.method, use.owner, use.prop)
            elif targets and targets & stretched:
                add("stretched-frame-property", use.method, use.owner, use.prop, targets & stretched)

        for key, (kind, owners, whats, clips, methods) in found.items():
            where, what = ", ".join(owners), ", ".join(whats)
            self._warn(key, {
                "unresolved-goto": f"unresolved receiver: {what} in {where} -- "
                                   "if it targets a stretched clip, check by hand",
                "unresolved-frame-property": f"{what} used in {where} on an unresolved receiver",
                "stretched-frame-property": f"{what} used in {where} on stretched clips {sorted(clips)}",
            }[kind], methods)

    # --- Interpolation ---

    def _plan_interpolation(self, frame_scripts, literal_gotos):
        plan = self.plan
        for character_id, retiming in sorted(plan.sprite_retiming.items()):
            if retiming.ratio == 1:
                continue    # no frames in between to interpolate
            sprite = self.movie.sprites[character_id]
            steps, stats = find_tweens(simulate_timeline(sprite.tags))
            if not steps:
                continue
            samples = sample_tweens(steps, retiming, self._anchors(sprite, frame_scripts, literal_gotos))
            moves = tween_moves(sprite.tags, samples, retiming)
            added_bytes = sum(len(move.to_bytes()) for tick_moves in moves.values() for move in tick_moves)
            motion = sum(change_magnitude(step.start, step.end) for frame_steps in steps.values() for step in frame_steps)
            plan.tweens[character_id] = SpriteTweens(steps, samples, moves, stats, added_bytes, motion)

    def _anchors(self, sprite, frame_scripts, literal_gotos) -> set[int]:
        """The frames where playback may start, stop or jump to, which tweens must show exactly: labels,
        frame scripts (stop() and others) and the targets of literal gotos"""
        scripts = frame_scripts.get(self.movie.class_name(sprite.character_id))
        anchors = set(sprite.labels) | (scripts.frames if scripts else set())
        for call, targets in literal_gotos:
            if sprite.character_id in targets:
                anchors.add(int(call.arg.value) - 1)
        return anchors
