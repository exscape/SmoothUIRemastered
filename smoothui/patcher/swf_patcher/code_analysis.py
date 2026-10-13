"""Heuristic scans of the ActionScript code for things that depend on frame numbers or on the frame rate.

Everything here works on single methods, with a minimal stack model: it can tell what a simple
expression like `this.icon.gotoAndStop(3)` refers to, and gives up ("computed", "unresolved") on
anything more complex. The planner treats a give-up as "not safe to stretch".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .abc_bytecode import (
    INTEGER_PUSH_NAMES,
    SKIPPABLE_NAMES,
    Instruction,
    branch_targets,
    expression_start,
    integer_constant,
    local_register,
    skip_no_ops_backwards,
)
from .movie import SwfMovie

GOTO_METHODS = ("gotoAndStop", "gotoAndPlay")
FRAME_PROPERTIES = ("totalFrames", "currentFrame", "nextFrame", "prevFrame", "framesLoaded")

# Instructions that can appear in a receiver expression without changing which object it names
NAME_PRESERVING = frozenset({
    "coerce", "coerce_a", "convert_o", "getlocal0", "getlocal1", "getlocal2", "getlocal3", "getlocal",
    "findpropstrict", "getscopeobject", "getslot", "pushscope",
})


class ArgKind(Enum):
    LABEL = "label"         # a frame label (string): unaffected by stretching
    NUMBER = "number"       # a literal frame number
    COMPUTED = "computed"   # a frame number computed at runtime (or unknown)


@dataclass(frozen=True)
class FrameArg:
    kind: ArgKind
    value: int | None = None    # for NUMBER: the 1-based frame number


class ReceiverKind(Enum):
    THIS = "this"               # the object running the code
    NAMED = "named"             # a child instance, by instance name
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Receiver:
    kind: ReceiverKind
    names: frozenset = frozenset()

THIS = Receiver(ReceiverKind.THIS)
UNRESOLVED = Receiver(ReceiverKind.UNRESOLVED)


@dataclass
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


@dataclass
class FrameScripts:
    """The addFrameScript(frame, fn, ...) calls in a class constructor"""
    method: int
    calls: list[list[tuple[int, int]]]  # per call: [(push instruction index, 0-based frame)]
    recognised: bool        # every call had the expected shape, so its frame numbers can be rewritten


@dataclass
class FrameHandler:
    """A method registered as an ENTER_FRAME listener"""
    method: int
    owner: str
    one_shot: bool          # removes itself
    time_based: bool        # uses getTimer(), so it is frame rate independent
    # Per-frame `obj.prop += K` / `-= K`: (index of the instruction pushing K, K, property name)
    increments: list[tuple[int, float, str]] = field(default_factory=list)


@dataclass
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
            # Something like a ternary: guess from the instructions just before the call
            window = _significant(code[max(0, i - 24):i])[-8:]
            has_number = any(w.name in (*INTEGER_PUSH_NAMES, "pushint", "pushuint") for w in window)
            has_string = any(w.name == "pushstring" for w in window)
            if has_string and not has_number:
                site.arg = FrameArg(ArgKind.LABEL)
            return site

        site.arg = self._classify_frame_arg(method, code[arg_start:i])
        site.receiver = self._resolve_receiver(code, receiver_start, arg_start)
        if site.arg.kind == ArgKind.NUMBER:
            site.push_index = skip_no_ops_backwards(code, i - 1)
        return site

    def _classify_frame_arg(self, method, expression) -> FrameArg:
        """Is the goto argument a label, a literal frame number, or computed?"""
        abc = self.abc
        span = _significant(expression)
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
            if last.name.startswith("getlocal"):
                # A parameter: a label if it is declared as a String
                register = local_register(last)
                param_types = abc.methods[method].param_types
                if 1 <= register <= len(param_types) and param_types[register - 1] \
                        and abc.name_of(param_types[register - 1]) == "String":
                    return FrameArg(ArgKind.LABEL)
                return FrameArg(ArgKind.COMPUTED)

        is_label = (
            (last.name in ("getproperty", "getlex") and self.name(last) in self.movie.string_properties)
            or last.name in ("coerce_s", "convert_s")
            or (last.name == "add" and any(ins.name == "pushstring" for ins in span))
            or (last.name in ("callproperty", "callproplex") and self.name(last) in self.movie.string_methods)
        )
        return FrameArg(ArgKind.LABEL if is_label else ArgKind.COMPUTED)

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

    # --- addFrameScript ---

    def find_frame_scripts(self) -> dict[str, FrameScripts]:
        """Class name -> the addFrameScript calls in its constructor"""
        found = {}
        for instance in self.abc.instances:
            method = instance.constructor
            code = self.movie.code.get(method)
            if not code:
                continue
            calls, recognised = [], True
            for i, ins in enumerate(code):
                if ins.name == "callpropvoid" and self.name(ins) == "addFrameScript":
                    pairs, call_recognised = self._frame_script_args(code, i)
                    recognised = recognised and call_recognised
                    calls.append(pairs)
            if calls:
                found[self.abc.full_name_of(instance.name)] = FrameScripts(method, calls, recognised)
        return found

    def _frame_script_args(self, code, call_index) -> tuple[list[tuple[int, int]], bool]:
        """Find the frame numbers of addFrameScript(frame, this.fn, frame, this.fn, ...).

        Returns ([(push instruction index, frame)], recognised). If the arguments don't have exactly
        that shape, recognised is False and the list holds the pairs found before giving up."""
        arg_count = code[call_index].args[1]
        pairs = []
        complete = True
        j = call_index - 1
        for _ in range(arg_count // 2):
            # The function: getlocal0; getproperty/getlex <name>
            j = skip_no_ops_backwards(code, j)
            if not (j >= 1 and code[j].name in ("getproperty", "getlex") and code[j - 1].name == "getlocal0"):
                complete = False
                break
            j = skip_no_ops_backwards(code, j - 2)
            # The frame number
            frame = integer_constant(self.abc, code[j]) if j >= 0 else None
            if frame is None:
                complete = False
                break
            pairs.append((j, frame))
            j -= 1
        pairs.reverse()

        recognised = complete and pairs and arg_count % 2 == 0
        if recognised:
            # No branch may jump into the middle of the argument list
            first, last = code[pairs[0][0]].offset, code[call_index].offset
            recognised = not any(first < target <= last for target in branch_targets(code))
        return pairs, bool(recognised)

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

    # --- totalFrames, currentFrame, ... ---

    def find_frame_property_uses(self) -> list[FramePropertyUse]:
        uses = []
        for method, code in self.movie.code.items():
            for i, ins in enumerate(code):
                if ins.name not in ("getproperty", "getlex", "callproperty", "callpropvoid"):
                    continue
                prop = self.name(ins)
                if prop not in FRAME_PROPERTIES:
                    continue
                uses.append(FramePropertyUse(method, prop, self.movie.owner_name(method),
                                             self.movie.class_of_method.get(method),
                                             self._frame_property_receiver(code, i)))
        return uses

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
