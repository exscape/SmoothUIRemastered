"""The merger window's own texts, and what its status card shows (status_card), together so that they can
be reviewed in one place. The library's messages stay with the library (state.py, policy.py, ...), the
screens' names in screens.py.

No Tk, and nothing that patches (the merger's window imports this at once): tests run without a window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .installed import Status
from .screens import screen_name
from .state import Check, FileState, Severity, State

if TYPE_CHECKING:
    from .builder import BuildResult

# The card's tones (its color and glyph)
NEUTRAL, OK, INFO, WARNING, ERROR = "neutral", "ok", "info", "warning", "error"


@dataclass(frozen=True, slots=True)
class Card:
    """What the status card shows. A button is (label, action); the first one is the default. Actions:
    merge, merge_and_play, play, abort, remove, remove_and_play, rescan, bug_report, smoothui_page."""
    tone: str
    headline: str
    sentences: tuple[str, ...] = ()
    buttons: tuple[tuple[str, str], ...] = ()
    busy: bool = False                  # something runs: show a progress bar
    progress: float | None = None       # busy: how far (0 to 1); None: unknown


# Buttons used on several cards
RESCAN = ("Re-scan", "rescan")
PLAY = ("Play", "play")
PLAY_ANYWAY = ("Play anyway", "play")
DONT_START = ("Don't start the game", "abort")

# A merge is needed: by what playing without it costs (state.Severity)
HEADLINES = {
    Severity.HIGH: "Your merged patches are out of date",
    Severity.MEDIUM: "Some screens aren't patched",
    Severity.LOW: "Merging again is recommended",
}
COST = {
    Severity.HIGH: "Until you merge, those screens may look wrong or not work.",
    Severity.MEDIUM: "Until you merge, those screens run at their original frame rate.",
    Severity.LOW: "Your game works as it is.",
}
PLAY_WITHOUT_MERGING = {
    Severity.HIGH: "Play without merging (may break mods)",
    Severity.MEDIUM: "Play without merging (unpatched screens)",
    Severity.LOW: "Play without merging",
}
TONES = {Severity.HIGH: ERROR, Severity.MEDIUM: WARNING, Severity.LOW: INFO}

# One sentence per cause, for the screens it concerns ({screens}: their names). A cause with {mod} is said once
# per mod (whose copies they are), one without it once. Keys: FileState.cause, or its state's value; and
# policy.FileStatus's values, with "fixes" for a copy patched without some fixes.
CAUSES = {
    "smoothui": "Smooth UI was updated.",
    "merger": "The merger was updated.",
    "damaged": "The merged patches are damaged.",
    "entry": "The merged patches' mods.settings entry is missing or changed.",
    "updated": "{mod} was updated: {screens}.",
    "load order": "{mod} comes first now: {screens}.",
    "not built": "{mod} isn't merged yet: {screens}.",
    "no longer needed": "No other mod changes these now: {screens}.",
    "overridden": "{mod} has Priority 0 too, and its name sorts first: {screens}.",
    "undetermined": "The load order doesn't decide which mod's copy the game uses: {screens}.",
    "left out": "{mod}'s copy can't be fully patched: {screens}.",
    "failed": "{mod} can't be patched: {screens}.",
    "forced": "{mod}'s copy is patched anyway, as you chose: {screens}.",
    "fixes": "{mod}'s copy is patched without some fixes: {screens}.",
}
MORE_CAUSES = "And {count} more: see the list below."
MAX_CAUSES = 2          # sentences about causes on a card, the last one perhaps MORE_CAUSES
MAX_SCREENS = 4         # screens named in a sentence; then "and N more"


def status_card(found: Check | None, *, stale: bool, busy: str | None = None, scan_error: str | None = None,
                merged: BuildResult | None = None, merge_error: str | None = None,
                progress: tuple[int, int] | None = None) -> Card:
    """What the status card shows, from the window's state. found: the last check (None until one ends);
    stale: whether the launcher opened the window; busy: what runs in a thread ("scan", "build" or
    "remove"); scan_error: why the last check failed; merged and merge_error: the result of the window's
    last merge, or why it failed; progress: the merge's (done, total), once known.

    In stale mode, every card has a button that plays, except while merging or removing."""
    if busy == "build":
        return _merging(stale, progress)
    if busy == "remove":
        return Card(NEUTRAL, "Removing the merged patches...", busy=True)
    if busy == "scan" or (found is None and scan_error is None):
        buttons = (("Play without waiting", "play"), DONT_START) if stale else ()
        return Card(NEUTRAL, "Checking your mods...", buttons=buttons, busy=True)
    if scan_error is not None:
        return Card(ERROR, "Can't read your mods", (_sentence(scan_error),), (PLAY, DONT_START) if stale else (RESCAN,))
    if merge_error is not None:
        if stale:
            play = PLAY_WITHOUT_MERGING[found.severity] if found and found.severity else "Play without merging"
            buttons = (("Merge again", "merge_and_play"), (play, "play"), DONT_START)
        else:
            buttons = (("Merge again", "merge"), ("Save a bug report", "bug_report"))
        return Card(ERROR, "Merging failed", (_sentence(merge_error),), buttons)
    if merged is not None:
        return _merged(merged, stale)
    return _checked(found, stale)


def _merging(stale, progress):
    if progress is None:
        headline, fraction = "Merging...", None
    elif progress[0] < progress[1]:
        headline, fraction = f"Merging: {progress[0]} of {_count(progress[1], 'screen')}", progress[0] / progress[1]
    else:
        headline, fraction = "Writing the merged patches...", 1.0
    return Card(NEUTRAL, headline, ("The game starts when it's done.",) if stale else (), busy=True, progress=fraction)


def _merged(result: BuildResult, stale):
    """After a merge in the window"""
    if result.clean:
        included = [(file.mod, file.path) for file in result.files if file.status.included]
        headline = f"Merged: {_patched(included)}" if included else "Merged: your mods' copies are already smooth"
        return Card(OK, headline, (), (PLAY,) if stale else (RESCAN,))
    causes = []
    for file in result.files:
        status = file.status.value
        if status in ("left out", "failed", "mod failed", "forced"):
            causes.append(("failed" if status == "mod failed" else status, file.mod, file.path))
        elif file.fixes_not_applied:
            causes.append(("fixes", file.mod, file.path))
    notes = []
    if result.check_problems:
        notes.append("Some merged screens don't load as they should: see the notes below.")
    if result.problems or result.installation.written.unknown_files:
        notes.append("See the notes below.")
    headline = ("Merged, but some screens couldn't be patched" if any(file.severity == "error" for file in result.files)
                else "Merged, with problems")
    return Card(WARNING, headline, _sentences(causes, notes), (PLAY, DONT_START) if stale else (RESCAN,))


def _checked(found: Check, stale):
    """From the last check"""
    installed = found.scan.installed
    if found.package_without_smoothui:
        if installed.status in (Status.NOT_INSTALLED, Status.DISABLED):
            disabled = installed.status == Status.DISABLED
            sentences = ("Without Smooth UI, they may break your mods' screens.",
                         f"Remove them, or {'enable' if disabled else 'install'} Smooth UI again.")
            buttons = ((("Remove them and play", "remove_and_play"), PLAY_ANYWAY, DONT_START) if stale
                       else (("Remove the merged patches", "remove"), RESCAN))
            return Card(ERROR, f"Smooth UI is {'disabled' if disabled else 'not installed'}, but its merged patches "
                        "are still installed", sentences, buttons)
        return Card(ERROR, "The merged patches don't fit the installed Smooth UI", (_sentence(installed.message),),
                    (PLAY_ANYWAY, DONT_START) if stale else (RESCAN,))
    if not installed.usable:
        if found.scan.conflicts:
            sentences = (_sentence(installed.message),
                         f"Until then, these mods' screens may look wrong: {_join(found.scan.conflicts)}.")
            return Card(ERROR, "Smooth UI can't be used with your mods right now", sentences,
                        (PLAY_ANYWAY, DONT_START) if stale else (RESCAN,))
        return _unusable(installed, stale)

    if found.stale:
        files = sorted((file for file in found.states if file.severity is not None), key=lambda file: -file.severity)
        causes = [(_cause(file), file.winner, file.path) for file in files]
        if not found.needs_build:
            # Only another mod at Priority 0 that sorts first: a merge changes nothing
            fix = "Give it a priority above 0 (in your mod manager or mods.settings), then re-scan."
            return Card(WARNING, "Another mod loads before the merged patches", _sentences(causes, [fix]),
                        (PLAY, DONT_START) if stale else (RESCAN,))
        severity = found.severity
        buttons = ((("Merge and play", "merge_and_play"), (PLAY_WITHOUT_MERGING[severity], "play"), DONT_START) if stale
                   else (("Merge", "merge"), RESCAN))
        return Card(TONES[severity], HEADLINES[severity], _sentences(causes, [COST[severity]]), buttons)

    # Up to date: what the merged patches hold, and what they can't
    causes = [(_cause(file), file.winner, file.path) for file in found.states
              if file.state == State.UNDETERMINED or (file.state == State.NOT_HELD and file.cause != "unchanged")]
    patched = [(file.winner, file.path) for file in found.states if file.state == State.UP_TO_DATE]
    buttons = (PLAY,) if stale else (RESCAN, ("Merge again", "merge"))
    if causes:
        return Card(WARNING, "Some screens can't be patched", _sentences(causes), buttons)
    if patched:
        return Card(OK, "Your merged patches are up to date", (f"They patch {_patched(patched)}.",), buttons)
    if any(file.state == State.NOT_HELD for file in found.states):
        return Card(OK, "Nothing to merge", ("Your other mods' copies of Smooth UI's screens are already smooth.",),
                    (PLAY,) if stale else (RESCAN,))
    return Card(OK, "Nothing to merge", ("No other mod changes Smooth UI's screens.",), (PLAY,) if stale else (RESCAN,))


def _unusable(installed, stale):
    """Smooth UI can't be used, and nothing installed depends on it (in stale mode, the launcher doesn't
    open the window then; a re-scan may find it)"""
    if installed.status == Status.NOT_INSTALLED:
        card = Card(WARNING, "Smooth UI Remastered isn't installed",
                    ("Install it, then re-scan.", "Using Mod Organizer 2? Start the merger from it."),
                    (("Get Smooth UI Remastered", "smoothui_page"), RESCAN))
    elif installed.status == Status.DISABLED:
        card = Card(INFO, "Smooth UI Remastered is disabled",
                    ("Enable it (in your mod manager, for example), then re-scan.",), (RESCAN,))
    else:
        headline = {Status.TOO_OLD: "Smooth UI Remastered is too old",
                    Status.TWO_FOLDERS: "Two Smooth UI folders are installed"}.get(installed.status,
                                                                                    "Unknown Smooth UI build")
        card = Card(WARNING, headline, (_sentence(installed.message),), (RESCAN,))
    return replace(card, buttons=(PLAY, DONT_START)) if stale else card


def _cause(file: FileState) -> str:
    """The CAUSES key for a file in a check"""
    if file.state == State.OVERRIDDEN:
        return "entry" if file.needs_build else "overridden"
    if file.cause == "mod failed":
        return "failed"
    return file.cause or file.state.value


def _sentences(causes, notes=()) -> tuple[str, ...]:
    """One sentence per cause (see CAUSES), from causes ((key, mod, path), the most important first), then
    notes. Only MAX_CAUSES of them: the last one that fits then says how many more there are."""
    groups = {}
    for key, mod, path in causes:
        groups.setdefault((key, mod if "{mod}" in CAUSES[key] else None), []).append(path)
    sentences = [CAUSES[key].format(mod=mod, screens=_screens(paths)) for (key, mod), paths in groups.items()]
    if len(sentences) > MAX_CAUSES:
        sentences[MAX_CAUSES - 1:] = [MORE_CAUSES.format(count=len(sentences) - MAX_CAUSES + 1)]
    return (*sentences, *notes)


def _screens(paths) -> str:
    """The screens' names, e.g. "Inventory, Map and Gwent"; "..., and 3 more" past MAX_SCREENS"""
    names = list(dict.fromkeys(screen_name(path) for path in paths))
    if len(names) > MAX_SCREENS:
        names[MAX_SCREENS - 1:] = [f"{len(names) - MAX_SCREENS + 1} more"]
    return _join(names)


def _patched(files) -> str:
    """E.g. "10 screens from 5 mods", for files ((mod, path))"""
    return f"{_count(len(files), 'screen')} from {_count(len({mod.lower() for mod, _ in files}), 'mod')}"


def _join(names) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _count(number, noun) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _sentence(text) -> str:
    """A library message (e.g. "can't read ...: ...") as a sentence"""
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."
