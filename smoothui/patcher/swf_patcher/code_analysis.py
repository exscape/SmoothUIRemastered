"""Heuristic scans of the ActionScript code for things that depend on frame numbers or on the frame rate.

Everything here works on single methods, with a minimal stack model: it can tell what a simple
expression like `this.icon.gotoAndStop(3)` refers to, and gives up ("computed", "unresolved") on
anything more complex. The planner treats a give-up as "not safe to stretch".
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from enum import Enum

from .abc_bytecode import (
    BINARY,
    INTEGER_PUSH_NAMES,
    NO_OP_NAMES,
    OPCODES,
    SKIPPABLE_NAMES,
    UNARY,
    Instruction,
    branch_targets,
    expression_start,
    integer_constant,
    local_register,
    skip_no_ops_backwards,
    stack_effect,
)
from .abc_file import TRAIT_METHOD, VALUE_TRAIT_KINDS
from .movie import SwfMovie

GOTO_METHODS = ("gotoAndStop", "gotoAndPlay")
FRAME_PROPERTIES = ("totalFrames", "currentFrame", "nextFrame", "prevFrame", "framesLoaded")

# Instructions that compute a new value from their operands, without other effects
PURE_OPERATIONS = (UNARY | BINARY) - {"getslot", "newclass", "checkfilter"}

# Instructions that change a local register other than by setlocal
REGISTER_CHANGING = frozenset({"inclocal", "declocal", "inclocal_i", "declocal_i", "kill", "hasnext2"})

# The significant instructions of a frame script that only calls stop()
STOP_ONLY = [("getlocal0", None), ("pushscope", None), ("findpropstrict", "stop"), ("callpropvoid", "stop"),
             ("returnvoid", None)]

# Flash's motion library. A 3D transform on a timeline becomes code in the clip's constructor that
# builds a MotionBase, and an AnimatorFactory3D that plays it on the clip's child.
MOTION_PACKAGE = "fl.motion."
STATIC_MOTION_NAMES = frozenset({"MotionBase", "AnimatorFactory", "AnimatorFactory3D", "motion_internal"})

# Instructions that can appear in a receiver expression without changing which object it names
NAME_PRESERVING = frozenset({
    "coerce", "coerce_a", "convert_o", "getlocal0", "getlocal1", "getlocal2", "getlocal3", "getlocal",
    "findpropstrict", "getscopeobject", "getslot", "pushscope",
})


class ArgKind(Enum):
    LABEL = "label"         # a frame label (string): unaffected by stretching
    NUMBER = "number"       # a literal frame number
    COMPUTED = "computed"   # a frame number computed at runtime (or unknown)


@dataclass(frozen=True, slots=True)
class FrameArg:
    kind: ArgKind
    value: int | None = None    # for NUMBER: the 1-based frame number


class ReceiverKind(Enum):
    THIS = "this"               # the object running the code
    NAMED = "named"             # a child instance, by instance name
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class Receiver:
    kind: ReceiverKind
    names: frozenset = frozenset()

THIS = Receiver(ReceiverKind.THIS)
UNRESOLVED = Receiver(ReceiverKind.UNRESOLVED)


@dataclass(slots=True)
class GotoCall:
    """A gotoAndStop()/gotoAndPlay() call"""
    method: int
    index: int              # of the call instruction
    owner: str              # "pkg.Class.method"
    call: str               # "gotoAndStop" or "gotoAndPlay"
    class_name: str | None  # class whose method contains the call
    arg: FrameArg
    receiver: Receiver
    push_index: int | None = None   # for NUMBER args: the instruction pushing the frame number


@dataclass(slots=True)
class FrameScripts:
    """The addFrameScript(frame, fn, ...) calls in a class constructor"""
    method: int
    calls: list[list[tuple[int, int]]]  # per call: [(push instruction index, 0-based frame)]
    recognised: bool        # every call had the expected shape, so its frame numbers can be rewritten
    stop_frames: set[int] = field(default_factory=set)  # frames whose script only calls stop()

    @property
    def frames(self) -> set[int]:
        return {frame for call in self.calls for _, frame in call}


@dataclass(slots=True)
class FrameHandler:
    """A method registered as an ENTER_FRAME listener"""
    method: int
    owner: str
    one_shot: bool          # removes itself
    time_based: bool        # uses getTimer(), so it is frame rate independent
    # Per-frame `obj.prop += K` / `-= K`: (index of the instruction pushing K, K, property name)
    increments: list[tuple[int, float, str]] = field(default_factory=list)


@dataclass(slots=True)
class FramePropertyUse:
    """A read of totalFrames/currentFrame/..., or a nextFrame()/prevFrame() call"""
    method: int
    prop: str
    owner: str
    class_name: str | None
    receiver: Receiver


def _significant(instructions):
    return [ins for ins in instructions if ins.name not in SKIPPABLE_NAMES]


class CodeAnalysis:
    def __init__(self, movie: SwfMovie):
        self.movie = movie
        self.abc = movie.abc
        # A class with its own trace() would make `trace(x)` in its methods call that instead
        self._trace_is_overridden = any(self.abc.name_of(trait.name) == "trace"
                                        for instance, cls in zip(self.abc.instances, self.abc.classes)
                                        for trait in instance.traits + cls.traits)

    def name(self, ins: Instruction) -> str:
        """Name of the multiname operand of a property instruction"""
        return self.abc.name_of(ins.args[0])

    # --- gotoAndStop / gotoAndPlay ---

    def find_goto_calls(self) -> list[GotoCall]:
        calls = []
        for method, code in self.movie.code.items():
            targets = None
            for i, ins in enumerate(code):
                if ins.name not in ("callproperty", "callpropvoid") or self.name(ins) not in GOTO_METHODS:
                    continue
                if targets is None:
                    targets = branch_targets(code)
                calls.append(self._goto_call(method, code, i, targets))
        return calls

    def _goto_call(self, method, code, i, targets) -> GotoCall:
        ins = code[i]
        site = GotoCall(method, i, self.movie.owner_name(method, f"method#{method}"), self.name(ins),
                        self.movie.class_of_method.get(method), FrameArg(ArgKind.COMPUTED), UNRESOLVED)
        if ins.args[1] != 1:    # a scene argument as well
            return site

        arg_start = expression_start(self.abc, code, i)
        receiver_start = expression_start(self.abc, code, arg_start) if arg_start is not None else None
        branches_into_args = arg_start is None or receiver_start is None or any(
            code[receiver_start].offset < target <= ins.offset for target in targets)
        if branches_into_args:
            arms = self._ternary_arms(code, i, targets)
            if arms and all(self._classify_frame_arg(method, code, *arm).kind == ArgKind.LABEL for arm in arms):
                site.arg = FrameArg(ArgKind.LABEL)
                return site
            # Something more complex: guess from the instructions just before the call
            window = _significant(code[max(0, i - 24):i])[-8:]
            has_number = any(w.name in (*INTEGER_PUSH_NAMES, "pushint", "pushuint") for w in window)
            has_string = any(w.name == "pushstring" for w in window)
            if has_string and not has_number:
                site.arg = FrameArg(ArgKind.LABEL)
            return site

        site.arg = self._classify_frame_arg(method, code, arg_start, i)
        site.receiver = self._resolve_receiver(code, receiver_start, arg_start)
        if site.arg.kind == ArgKind.NUMBER:
            site.push_index = skip_no_ops_backwards(code, i - 1)
        return site

    def _classify_frame_arg(self, method, code, start, end) -> FrameArg:
        """Is the goto argument code[start:end] a label, a literal frame number, or computed?"""
        abc = self.abc
        span = _significant(code[start:end])
        if not span:
            return FrameArg(ArgKind.COMPUTED)
        last = span[-1]
        if last.name == "pushstring" or span[0].name == "pushstring":
            return FrameArg(ArgKind.LABEL)

        if len(span) == 1:
            value = integer_constant(abc, last)
            if value is None and last.name == "pushuint":
                value = abc.uints[last.args[0]]
            if value is not None:
                return FrameArg(ArgKind.NUMBER, value)

        is_label = (self.static_type(method, code, start, end) == "String"
                    or (last.name == "add" and any(ins.name == "pushstring" for ins in span)))
        return FrameArg(ArgKind.LABEL if is_label else ArgKind.COMPUTED)

    def _ternary_arms(self, code, end, targets) -> list[tuple[int, int]] | None:
        """If the value consumed by code[end] is `condition ? a : b`, the (start, end) of a and b"""
        b_start = expression_start(self.abc, code, end)
        if b_start is None:
            return None
        b_last = skip_no_ops_backwards(code, end - 1)
        jump = skip_no_ops_backwards(code, b_start - 1)
        if jump < 0 or code[jump].name != "jump" \
                or code[jump].jump_targets()[0] not in {ins.offset for ins in code[b_last + 1:end + 1]}:
            return None
        a_start = expression_start(self.abc, code, jump)
        if a_start is None:
            return None
        branch = skip_no_ops_backwards(code, a_start - 1)
        if branch < 0 or not code[branch].is_branch \
                or code[branch].jump_targets()[0] not in {ins.offset for ins in code[jump + 1:b_start + 1]}:
            return None
        # Nothing else may branch into either arm
        if any(code[a_start].offset <= target <= code[jump].offset
               or code[b_start].offset < target <= code[b_last].offset for target in targets):
            return None
        return [(a_start, jump), (b_start, end)]

    def _resolve_receiver(self, code, start, end) -> Receiver:
        """Which object does the expression code[start:end] refer to?"""
        span = _significant(code[start:end])
        if not span:
            return UNRESOLVED
        if len(span) == 1 and span[0].name in ("getlocal0", "findpropstrict"):
            return THIS
        if len(span) == 1 and span[0].name.startswith("getlocal"):
            return self._resolve_local(code, start, local_register(span[0]))
        if any(ins.name in ("getscopeobject", "getslot") for ins in span):
            return UNRESOLVED
        return self._named(self.instance_name(code, start, end))

    def _resolve_local(self, code, before, register) -> Receiver:
        """The object stored in a local register by the nearest preceding assignment"""
        for j in range(before - 1, -1, -1):
            ins = code[j]
            if ins.name.startswith("setlocal") and local_register(ins) == register:
                start = expression_start(self.abc, code, j)
                if start is None:
                    return UNRESOLVED
                return self._named(self.instance_name(code, start, j))
        return UNRESOLVED

    @staticmethod
    def _named(name):
        return Receiver(ReceiverKind.NAMED, frozenset({name})) if name is not None else UNRESOLVED

    def instance_name(self, code, start, end) -> str | None:
        """The instance name the expression code[start:end] evaluates to (its last property name or
        string), if it is a simple property chain like this.panel.icon or getChildByName("icon")"""
        name = None
        tail_ok = True
        for ins in code[start:end]:
            if ins.name in SKIPPABLE_NAMES:
                continue
            if ins.name in ("getproperty", "getlex", "getsuper"):
                prop = self.name(ins)
                if prop.startswith("<"):
                    continue    # a runtime name: the string pushed before it is the name
                name, tail_ok = prop, True
            elif ins.name == "pushstring":
                name, tail_ok = self.abc.strings[ins.args[0]], True
            elif ins.name in ("callproperty", "callproplex"):
                called = self.name(ins)
                # getChildByName("x"), or a cast like MovieClip(x)
                tail_ok = called in ("getChildByName", "getChildAt") or called[:1].isupper()
            elif ins.name not in NAME_PRESERVING:
                tail_ok = False
        return name if tail_ok else None

    def find_play_calls(self) -> list[tuple[int, Receiver]]:
        """play() calls: (method, receiver)"""
        found = []
        for method, code in self.movie.code.items():
            for i, ins in enumerate(code):
                if ins.name in ("callproperty", "callpropvoid") and self.name(ins) == "play" and ins.args[1] == 0:
                    start = expression_start(self.abc, code, i)
                    found.append((method, UNRESOLVED if start is None else self._resolve_receiver(code, start, i)))
        return found

    # --- addFrameScript ---

    def find_frame_scripts(self) -> dict[str, FrameScripts]:
        """Class name -> the addFrameScript calls in its constructor"""
        found = {}
        for instance in self.abc.instances:
            method = instance.constructor
            code = self.movie.code.get(method)
            if not code:
                continue
            class_name = self.abc.full_name_of(instance.name)
            calls, recognised, stop_frames = [], True, set()
            for i, ins in enumerate(code):
                if ins.name == "callpropvoid" and self.name(ins) == "addFrameScript":
                    pairs, functions, call_recognised = self._frame_script_args(code, i)
                    recognised = recognised and call_recognised
                    calls.append(pairs)
                    stop_frames.update(frame for (_, frame), function in zip(pairs, functions)
                                       if self._only_stops(class_name, function))
            if calls:
                found[class_name] = FrameScripts(method, calls, recognised, stop_frames)
        return found

    def _only_stops(self, class_name, function) -> bool:
        """Does the frame script `function` (a method of class_name) only call stop()?"""
        methods = [trait.method for trait in self.movie.instance_of[class_name].traits
                   if trait.kind == TRAIT_METHOD and self.abc.name_of(trait.name) == function]
        code = _significant(self.movie.code.get(methods[0], [])) if len(methods) == 1 else []
        return [(ins.name, self.name(ins) if ins.name in ("findpropstrict", "callpropvoid") else None)
                for ins in code] == STOP_ONLY

    def _frame_script_args(self, code, call_index) -> tuple[list[tuple[int, int]], list[str], bool]:
        """Find the frame numbers of addFrameScript(frame, this.fn, frame, this.fn, ...).

        Returns ([(push instruction index, frame)], [fn name for each], recognised). If the arguments
        don't have exactly that shape, recognised is False and the lists hold what was found before
        giving up."""
        arg_count = code[call_index].args[1]
        pairs, functions = [], []
        complete = True
        j = call_index - 1
        for _ in range(arg_count // 2):
            # The function: getlocal0; getproperty/getlex <name>
            j = skip_no_ops_backwards(code, j)
            if not (j >= 1 and code[j].name in ("getproperty", "getlex") and code[j - 1].name == "getlocal0"):
                complete = False
                break
            function = self.name(code[j])
            j = skip_no_ops_backwards(code, j - 2)
            # The frame number
            frame = integer_constant(self.abc, code[j]) if j >= 0 else None
            if frame is None:
                complete = False
                break
            pairs.append((j, frame))
            functions.append(function)
            j -= 1
        pairs.reverse()
        functions.reverse()

        recognised = complete and pairs and arg_count % 2 == 0
        if recognised:
            # No branch may jump into the middle of the argument list
            first, last = code[pairs[0][0]].offset, code[call_index].offset
            recognised = not any(first < target <= last for target in branch_targets(code))
        return pairs, functions, bool(recognised)

    # --- ENTER_FRAME handlers ---

    def find_frame_handlers(self) -> list[FrameHandler]:
        """Methods passed to addEventListener(Event.ENTER_FRAME, handler)"""
        movie = self.movie
        by_class_and_name = {}  # (class, method name) -> method
        for method, owner in movie.method_owner.items():
            by_class_and_name.setdefault((movie.class_of_method.get(method), owner.split(".")[-1]), method)
        by_name = {}
        for (_, name), method in by_class_and_name.items():
            by_name.setdefault(name, method)

        handlers, seen = [], set()
        for method, code in movie.code.items():
            for i, ins in enumerate(code):
                if ins.name != "callpropvoid" or self.name(ins) != "addEventListener":
                    continue
                window = _significant(code[max(0, i - 10):i])
                names = [self.name(w) for w in window if w.name in ("getproperty", "getlex")]
                if "ENTER_FRAME" not in names:
                    continue
                # The handler is the property read right after Event.ENTER_FRAME
                after = len(names) - names[::-1].index("ENTER_FRAME")
                handler_name = names[after] if after < len(names) else None
                handler = (by_class_and_name.get((movie.class_of_method.get(method), handler_name))
                           or by_name.get(handler_name))
                if handler is None or handler in seen:
                    continue
                seen.add(handler)
                handlers.append(self._frame_handler(handler))
        return handlers

    def _frame_handler(self, method) -> FrameHandler:
        code = self.movie.code.get(method, [])
        calls = lambda name: any(ins.name == "callpropvoid" and self.name(ins) == name for ins in code)
        reads = lambda name, ops: any(ins.name in ops and self.name(ins) == name for ins in code)
        one_shot = calls("removeEventListener") and reads("ENTER_FRAME", ("getproperty", "getlex"))
        time_based = reads("getTimer", ("getlex", "findpropstrict"))
        return FrameHandler(method, self.movie.owner_name(method), one_shot, time_based,
                            self._property_increments(code))

    def _property_increments(self, code) -> list[tuple[int, float, str]]:
        """Find `obj.prop += K`: getproperty prop; push K; add/subtract; setproperty prop"""
        abc = self.abc
        significant = [(j, ins) for j, ins in enumerate(code) if ins.name not in SKIPPABLE_NAMES]
        found = []
        for p in range(len(significant) - 3):
            (_, get), (push_index, push), (_, op), (_, put) = significant[p:p + 4]
            if not (get.name == "getproperty" and put.name == "setproperty" and op.name in ("add", "subtract")
                    and self.name(get) == self.name(put)):
                continue
            value = integer_constant(abc, push)
            if value is None and push.name == "pushdouble":
                value = abc.doubles[push.args[0]]
            if value is not None:
                found.append((push_index, value, self.name(get)))
        return found

    # --- frame rate changes ---

    def find_frame_rate_writes(self) -> list[int]:
        """Methods that set a frameRate property (stage.frameRate = ...). The receiver's type isn't known,
        but nothing else in the game's movies has a frameRate to set."""
        return [method for method, code in self.movie.code.items()
                if any(ins.name in ("setproperty", "initproperty") and self.name(ins) == "frameRate" for ins in code)]

    # --- totalFrames, currentFrame, ... ---

    def find_frame_property_uses(self) -> list[FramePropertyUse]:
        """Reads of the frame properties, and nextFrame()/prevFrame() calls. Left out: calls with
        arguments (MovieClip's take none, so these are some other class's methods), and reads whose
        value only ends up in a trace() call"""
        uses = []
        for method, code in self.movie.code.items():
            for i, ins in enumerate(code):
                if ins.name not in ("getproperty", "getlex", "callproperty", "callpropvoid"):
                    continue
                prop = self.name(ins)
                if prop not in FRAME_PROPERTIES:
                    continue
                if ins.name.startswith("call") and ins.args[1] != 0:
                    continue
                if ins.name in ("getproperty", "getlex") and self._only_traced(code, i):
                    continue
                uses.append(FramePropertyUse(method, prop, self.movie.owner_name(method),
                                             self.movie.class_of_method.get(method),
                                             self._frame_property_receiver(code, i)))
        return uses

    def _only_traced(self, code, i) -> bool:
        """Is the value that code[i] pushes only used in a trace() call (possibly after being turned
        into a string or computed with)?"""
        if self._trace_is_overridden:
            return False
        depth = 1   # the value's position on the stack, counted from the top
        for j in range(i + 1, len(code)):
            ins = code[j]
            if ins.name in NO_OP_NAMES:
                continue
            effect = stack_effect(self.abc, ins)
            if effect is None:
                return False
            popped, pushed = effect
            if depth > popped:
                depth += pushed - popped
                continue
            if ins.name in PURE_OPERATIONS:
                depth = 1   # follow the result instead
                continue
            # An argument (not the object) of a call to the global trace()
            return (ins.name == "callpropvoid" and self.name(ins) == "trace" and depth < popped
                    and self._is_global_trace(code, j))
        return False

    def _is_global_trace(self, code, call_index) -> bool:
        """Is the object of the callpropvoid trace at code[call_index] `findpropstrict trace`?"""
        start = call_index
        for _ in range(code[call_index].args[1] + 1):   # the arguments, then the object
            end, start = start, expression_start(self.abc, code, start)
            if start is None:
                return False
        span = _significant(code[start:end])
        return len(span) == 1 and span[0].name == "findpropstrict" and self.name(span[0]) == "trace"

    def _frame_property_receiver(self, code, i) -> Receiver:
        if code[i].name == "getlex":    # a bare `currentFrame` in a class method
            return THIS
        start = expression_start(self.abc, code, i)
        if start is None:
            return UNRESOLVED
        name = self.instance_name(code, start, i)
        if name is not None:
            return self._named(name)
        if i - start == 1 and code[start].name == "getlocal0":
            return THIS
        return UNRESOLVED

    # --- Static types ---

    def static_type(self, method, code, start, end) -> str | None:
        """The declared type ("String", "pkg.Class", ...) of the value of the expression
        code[start:end], if it can be told; None otherwise. Only simple expressions are understood:
        typed registers and activation slots, coercions, and properties and methods with a known type."""
        abc, movie = self.abc, self.movie
        indices = [j for j in range(start, end) if code[j].name not in SKIPPABLE_NAMES]
        if not indices:
            return None
        last = code[indices[-1]]
        if last.name in ("coerce_s", "convert_s", "pushstring"):
            return "String"
        if last.name in ("coerce_a", "convert_o") or (last.name == "coerce" and abc.full_name_of(last.args[0]) == "Object"):
            # These leave the value as it is; the arms of a ternary are coerced to their common type like this
            return self.static_type(method, code, start, indices[-1])
        if last.name == "coerce":
            return abc.full_name_of(last.args[0])
        if len(indices) == 1 and last.name.startswith("getlocal"):
            return self.register_type(method, code, local_register(last))
        if (len(indices) == 2 and code[indices[0]].name == "getscopeobject" and code[indices[0]].args[0] == 1
                and last.name == "getslot"):
            return self.activation_slot_type(method, code, last.args[0])
        if last.name in ("getproperty", "getlex") and not abc.runtime_name_operands(last.args[0]):
            prop = self.name(last)
            if last.name == "getproperty":
                receiver = self.static_type(method, code, start, indices[-1])
                if receiver and (found := movie.property_type(receiver, prop)):
                    return found
            return "String" if prop in movie.string_properties else None
        if last.name in ("callproperty", "callproplex") and self.name(last) in movie.string_methods:
            return "String"
        return None

    def register_type(self, method, code, register) -> str | None:
        """The declared type of a local register: `this`, a parameter, or a local variable that every
        assignment coerces to the same type"""
        abc, movie = self.abc, self.movie
        types = set()
        if register == 0:
            if method in movie.static_methods:
                return None     # the class itself
            types.add(movie.class_of_method.get(method))
        param_types = abc.methods[method].param_types
        if 1 <= register <= len(param_types):
            param_type = param_types[register - 1]
            types.add(abc.full_name_of(param_type) if param_type else None)
        for j, ins in enumerate(code):
            if ins.name in REGISTER_CHANGING and register in ins.args:
                return None
            if ins.name.startswith("setlocal") and local_register(ins) == register:
                types.add(self._assigned_type(code, j))
        return types.pop() if len(types) == 1 else None

    def _assigned_type(self, code, index) -> str | None:
        """The type the value stored by code[index] is coerced to (`coerce T; [dup;] setlocal`)"""
        j = skip_no_ops_backwards(code, index - 1)
        if j >= 0 and code[j].name == "dup":
            j = skip_no_ops_backwards(code, j - 1)
        if j < 0:
            return None
        if code[j].name in ("coerce_s", "convert_s"):
            return "String"
        if code[j].name == "coerce":
            return self.abc.full_name_of(code[j].args[0])
        return None

    def activation_slot_type(self, method, code, slot) -> str | None:
        """The declared type of a slot of the method's activation object (which holds the local
        variables that closures use). Slots are typed, so every value stored in one is coerced to
        its type. Only for methods whose scope pushes have the usual shape, where getscopeobject 1
        is always the activation: `this` and the activation, and in each exception handler, those
        two again and the exception."""
        pushes = self._scope_pushes(code)
        handlers = pushes[2:]
        if pushes[:2] != ["this", "activation"] or handlers != ["this", "activation", "catch"] * (len(handlers) // 3):
            return None
        body = self.abc.bodies.get(method)
        slots = [trait for trait in (body.traits if body else ()) if trait.kind in VALUE_TRAIT_KINDS]
        if any(trait.slot_id == 0 for trait in slots):
            return None     # slot ids assigned by position, which this doesn't handle
        types = {self.movie.value_type(trait) for trait in slots if trait.slot_id == slot}
        return types.pop() if len(types) == 1 else None

    def _scope_pushes(self, code) -> list[str | None]:
        """What each pushscope in the method pushes: "this", "activation", "catch" or None (other)"""
        code = _significant(code)
        registers = {local_register(c) for a, b, c in zip(code, code[1:], code[2:])     # newactivation; dup; setlocal N
                     if (a.name, b.name) == ("newactivation", "dup") and c.name.startswith("setlocal")}
        pushes = []
        for i, ins in enumerate(code):
            if ins.name in ("pushscope", "pushwith"):
                j = i - 1   # what is pushed, past the copies kept on the way (dup, dup; setlocal N)
                while j >= 1 and (code[j].name == "dup" or code[j].name.startswith("setlocal") and code[j - 1].name == "dup"):
                    j -= 1 if code[j].name == "dup" else 2
                name = code[j].name if j >= 0 and ins.name == "pushscope" else ""
                if name.startswith("getlocal") and name != "getlocal0" and local_register(code[j]) in registers:
                    name = "newactivation"
                pushes.append({"getlocal0": "this", "newactivation": "activation", "newcatch": "catch"}.get(name))
        return pushes

    # --- fl.motion ---

    def find_static_motion_hosts(self) -> set[str] | None:
        """The classes whose constructors set up fl.motion animations, if every motion in the file
        is static: a MotionBase with a duration of 1 frame, as Flash generates for a 3D transform
        that doesn't change. None if any motion may not be, or is made some other way (from XML,
        another class, or a name)."""
        movie, abc = self.movie, self.abc
        motion_names = {name.rsplit(".", 1)[-1] for name in movie.superclass if name.startswith(MOTION_PACKAGE)}
        motion_names.add("motion_internal")

        hosts = set()
        for method, code in movie.code.items():
            if movie.owner_name(method, "").startswith(MOTION_PACKAGE) or any(ins.name == "newclass" for ins in code):
                continue    # the library itself, and script initializers (which define the classes)
            if any(ins.name == "pushstring" and ("<Motion" in abc.strings[ins.args[0]] or "fl.motion" in abc.strings[ins.args[0]])
                   for ins in code):
                return None
            used = {self.name(ins) for ins in code if ins.name in ("getlex", "findpropstrict", "findproperty", "constructprop")
                    and self.name(ins) in motion_names}
            if not used:
                continue
            if used - STATIC_MOTION_NAMES:
                return None
            motions = [ins for ins in code if ins.name == "constructprop" and self.name(ins) == "MotionBase"]
            durations = [integer_constant(abc, code[skip_no_ops_backwards(code, j - 1)]) for j, ins in enumerate(code)
                         if ins.name == "setproperty" and self.name(ins) == "duration"]
            if any(ins.args[1] for ins in motions) or len(durations) != len(motions) or set(durations) - {1}:
                return None
            if method in movie.static_methods or method not in movie.class_of_method:
                return None
            hosts.add(movie.class_of_method[method])
        return hosts

    # --- Code identity ---

    def code_hash(self, methods) -> str:
        """A short hash (8 hex digits) of the methods' code, that only changes when the code does:
        debug information is left out, and constants, names and branch targets are resolved"""
        text = "\n\n".join(sorted(self._normalized_code(method) for method in set(methods)))
        return f"{zlib.crc32(text.encode()):08x}"

    def _normalized_code(self, method) -> str:
        code = self.movie.code.get(method, [])
        kept = [ins for ins in code if ins.name not in SKIPPABLE_NAMES]
        position = {code[-1].end if code else 0: len(kept)}  # code offset -> index in kept
        index = len(kept)
        for ins in reversed(code):
            if ins.name not in SKIPPABLE_NAMES:
                index -= 1
            position[ins.offset] = index    # a skipped instruction stands for the next one kept

        lines = []
        for ins in kept:
            if ins.is_branch or ins.is_lookupswitch:
                operands = [position[target] for target in ins.jump_targets()]
            else:
                operands = [self._operand(ins, k, value) for k, value in enumerate(ins.args)]
            lines.append(" ".join([ins.name, *map(str, operands)]))
        return "\n".join(lines)

    def _operand(self, ins: Instruction, k: int, value: int):
        """An instruction operand, with constant pool and multiname indices resolved"""
        abc = self.abc
        if k == 0 and OPCODES[ins.opcode][1] in ("m", "mu"):
            # Without the namespace: compilers refer to the same property in different ways
            return abc.name_of(value)
        pools = {"pushstring": abc.strings, "pushint": abc.ints, "pushuint": abc.uints, "pushdouble": abc.doubles,
                 "pushnamespace": [abc.strings[name] for _, name in abc.namespaces]}
        if ins.name in pools:
            return repr(pools[ins.name][value])
        if k == 0 and ins.name in ("newfunction", "newclass", "callmethod", "callstatic"):
            return "#"  # an index into the method or class list, which shifts when other code changes
        return value
