"""The merger window's own texts, and what its status card shows (status_card), together so that they can
be reviewed in one place. The library's messages stay with the library (state.py, policy.py, ...), the
screens' names in screens.py.

No Qt, and nothing that patches (the merger's window imports this at once): tests run without a window.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .installed import Status
from .scan import FileResult, is_own_folder
from .state import Check, FileState, Severity, State

if TYPE_CHECKING:
    from .builder import BuildResult

# The card's tones (its color and glyph); UPDATE: the notice of a newer version
NEUTRAL, OK, INFO, WARNING, ERROR, UPDATE = "neutral", "ok", "info", "warning", "error", "update"
# Glyphs by tone, from Segoe UI Symbol (in Windows 10 and later)
GLYPHS = {NEUTRAL: "", OK: "✓", INFO: "ⓘ", WARNING: "⚠", ERROR: "✕", UPDATE: "⬆"}

# ---- The window ----

TITLE = "Smooth UI Merger"
SCREENS_HEADING = "Screens other mods change"
SCREENS_NOTE = "Merged: the mod's copy of the screen now runs at Smooth UI's frame rate, with Smooth UI's fixes."
NOTES_HEADING = "Notes"
SHOW_LOG, HIDE_LOG = "Show log", "Hide log"
# Notices, above the card
TEST_MODE = "Test mode: {names} set (their values are in merger.log)"
NEWER_RELEASE = "A newer Smooth UI Remastered is available ({version})"
UPDATE_AVAILABLE = "Smooth UI Tools v{version} is available."
WHATS_NEW, UPDATE_NOW, LATER = "What's new", "Update now", "Later"
# A message box, by stale mode (the game starts once it closes)
_MO2 = ("Smooth UI Tools can't run from inside the game folder when Mod Organizer 2 starts them: MO2 would move "
        "their own files (settings, logs, updates) into its overwrite folder.\n\nMove the folder {folder} out of the "
        "game folder, then point MO2's executables to the new place.")
MO2_INSIDE_GAME = {False: _MO2, True: _MO2 + "\n\nThe game starts now, without the merger."}
# The What's new dialog: the release notes of each newer version, newest first
WHATS_NEW_TITLE = "What's new in Smooth UI Tools v{version}"
RELEASE_HEADING = "Smooth UI Tools v{version}"
NO_NOTES = "No release notes."
UPDATE_NOTE = "Updating takes a few seconds: the merger closes, and opens again in the new version."
UPDATED = "Updated to Smooth UI Tools v{version}."
# While updating: the status card (the download), and a message box if it fails (nothing has changed then)
DOWNLOADING_UPDATE = "Downloading Smooth UI Tools v{version}..."
UPDATE_FAILED_TITLE = "Update failed"
UPDATE_FAILED = ("Smooth UI Tools v{version} couldn't be installed: {error}\n\nNothing has changed. Try again "
                 "later, or download the new version from Nexus Mods.")
# Message boxes of the helper that swaps the files, after the merger has closed
UPDATE_NOT_INSTALLED = ("Smooth UI Tools couldn't be updated: {error}\n\nThe old version is still installed. Close "
                        "SmoothUILauncher.exe, the merger and the game, then start the merger again.")
UPDATE_START_FAILED = ("Smooth UI Tools were updated, but the new version didn't start: {error}\n\nStart "
                       "SmoothUIMerger.exe in {folder} yourself.")
# Notes, below the screens
COMPAT_REMOVABLE = "{folder} is no longer needed: the merged patches replace it. You can remove it."
UNKNOWN_FILES = "The merged patches' folder holds files the merger didn't write, so they stay: {names}"

REMOVE_TITLE = "Remove the merged patches?"
REMOVE_QUESTION = "Remove the merged patches from your Mods folder? Their mods.settings entry stays."
REMOVE_FAILED = "The merged patches can't be removed: {error}"
STOP_MERGE_TITLE = "Stop merging?"
# By stale mode (the game starts once the window closes)
STOP_MERGE = {
    False: ("Merging isn't done yet. Closing now stops it halfway, so the merged patches are deleted (half-written "
            "ones could break the game).\n\nOnly do this if the merger seems stuck.\n\nClose anyway?"),
    True: ("Merging isn't done yet. Closing now stops it halfway, so the merged patches are deleted (half-written "
           "ones could break the game), and the game starts without them.\n\nOnly do this if the merger seems "
           "stuck.\n\nClose anyway?"),
}
# The list
ALSO_CHANGED = "Also changed by {mods}; the game uses {mod}'s copy, which comes first in the load order."
PATCH_ANYWAY = "Merge anyway"
PATCH_ANYWAY_NOTE = "It may then look or behave wrong in places."
REVERT = "Revert to the mod's own copy"
REVERT_NOTE = "The game then shows {mod}'s own copy, at its original frame rate."

DELETE_FAILED = "The merged patches can't be deleted: {error}\n\nDelete the folder {folder} yourself before you play."
FAILED = "Smooth UI Merger failed:\n\n{error}\n\nDetails are in data\\{log}."

# ---- The welcome page (first run) and the settings page ----

WELCOME_HEADING = "Welcome to Smooth UI Merger"
WELCOME = ("Many UI mods change the same screens as Smooth UI Remastered. The game then shows their copies, "
           "without Smooth UI's improved frame rate. The merger adds Smooth UI's changes to those copies, in a mod "
           "of its own ({folder}) that the game loads first. Your mods themselves are never changed.")
CONTINUE = "Continue"
SETTINGS = "Settings"
SAVE, CANCEL = "Save", "Cancel"
# The game folder
GAME_HEADING = "Your game"
LOOKING_FOR_GAME = "Looking for the game..."
GAME_NOT_FOUND = "The game wasn't found: choose its folder."
CHOOSE_FOLDER = "Choose another folder..."
CHOOSE_FOLDER_TITLE = "Choose the game's folder (the one that holds bin, content and Mods)"
SETTINGS_NOT_SAVED = "The settings can't be saved: {error}"
# The launcher
LAUNCHER_HEADING = "Starting the game"
LAUNCHER = "Start the game with SmoothUILauncher.exe: it checks your mods first, and opens the merger if they changed."
SETUP_LINK = "How to set it up with your mod manager ↗"
CREATE_SHORTCUT = "Create launcher shortcut"
SHORTCUT_NAME = "The Witcher 3 (Smooth UI)"
SHORTCUT_DESCRIPTION = "Starts The Witcher 3 through SmoothUILauncher.exe, which checks your mods first"
SHORTCUT_MADE = "Made on your desktop: {name}"
NO_LAUNCHER = "SmoothUILauncher.exe isn't next to the merger: unpack all of Smooth UI Tools into one folder"
# The update check
CHECK_UPDATES = "Check for updates when the merger starts (contacts GitHub)"
# The data folder
DATA_HEADING = "Data folder"
DATA = "The merger's settings, logs and records of the merged patches:\n{folder}"
OPEN_DATA = "Open the data folder"

# ---- The help page, the log folder and the bug report ----

HELP = "Help"
BACK = "Back"
VERSIONS_HEADING = "Versions"
MERGER_VERSION = "Smooth UI Merger {version}"
SMOOTHUI_NOT_CHECKED = "Smooth UI Remastered: not checked yet"
GUIDE_HEADING = "Setting up"
GUIDE_LINK = "How to set up and use Smooth UI's tools ↗"
BUG_HEADING = "Reporting a bug"
BUG = ("A bug report lists your versions, what this window shows, the mods that change Smooth UI's screens, and "
       "the last warnings in the log. You can read and edit all of it before you copy it. Nothing is sent from here.")
REPORT_LINK = "Report a bug ↗"
WRITE_BUG_REPORT = "Write a bug report..."
BUG_DIALOG = ("This is everything the report says. Change it as you like (for example, to leave out a mod's name), "
              "then copy it into your bug report.")
CLOSE = "Close"
COPY_REPORT = "Copy to clipboard"
REPORT_COPIED = "Copied: paste it into your bug report."
ZIP_NOTE = ("If you're asked for more, save the full report: a zip with the merger's logs and settings, and your "
            "mods.settings (so it lists your mods). Your Windows user name is removed.")
SAVE_ZIP = "Save the full report (zip)"
BUG_REPORT_TITLE = "Bug report"
BUG_REPORT_FAILED = "The bug report can't be saved: {error}"
LICENSES_HEADING = "Licenses"
LICENSES = "Smooth UI's tools are free software (MIT license). The licenses of what they include are in their licenses folder."
OPEN_LICENSES = "Open the licenses folder"
OPEN_LOG_FOLDER = "Open log folder"


@dataclass(frozen=True, slots=True)
class Card:
    """What the status card shows. A button is (label, action); the first one is the default. Actions:
    merge, merge_and_play, play, abort, remove, remove_and_play, rescan, bug_report, smoothui_page, update."""
    tone: str
    headline: str
    sentences: tuple[str, ...] = ()
    buttons: tuple[tuple[str, str], ...] = ()
    default: bool = True                # whether the first button is the default (Enter presses it)
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
    Severity.MEDIUM: "Some screens aren't merged",
    Severity.LOW: "Merging again is recommended",
}
# What playing without merging costs, where the headline doesn't say it
COST = {
    Severity.HIGH: "Until you merge, those screens may look wrong or not work.",
    Severity.LOW: "Your game works as it is.",
}
PLAY_WITHOUT_MERGING = {
    Severity.HIGH: "Play without merging (may break mods)",
    Severity.MEDIUM: "Play without merging (screens not merged)",
    Severity.LOW: "Play without merging",
}
TONES = {Severity.HIGH: ERROR, Severity.MEDIUM: WARNING, Severity.LOW: INFO}

# Causes are keyed by FileState.cause, or its state's value, and by policy.FileStatus's values, with "fixes"
# for a copy merged without some fixes. The card names no screens: the list below it does. Those about
# mods' copies name the mods: one sentence per kind (MOD_CAUSES), from MODS (for one mod, for more).
MOD_CAUSES = {"updated": "changed", "load order": "changed", "no longer needed": "changed", "not built": "not merged",
              "left out": "left out", "failed": "left out", "fixes": "fixes"}
MODS = {
    "changed": ("{count} changed since the last merge: {mods}.", "{count} changed since the last merge: {mods}."),
    "not merged": ("{count} contains screens that aren't merged: {mods}.",
                 "{count} contain screens that aren't merged: {mods}."),
    "left out": ("{count} has screens that can't be fully merged: {mods}.",
                 "{count} have screens that can't be fully merged: {mods}."),
    "fixes": ("{count} has screens merged without some fixes: {mods}.",
              "{count} have screens merged without some fixes: {mods}."),
}
NUMBERS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")
# The other causes, one sentence each: one with {mod} once per mod, one without it once
CAUSES = {
    "smoothui": "Smooth UI was updated.",
    "merger": "The merger was updated.",
    "damaged": "The merged patches are damaged.",
    "entry": "The merged patches' mods.settings entry is missing or changed.",
    "overridden": "{mod} has priority 0 too, and its name sorts first.",
    "undetermined": "The load order doesn't decide which mod's copy the game uses for some screens.",
}
# What a Smooth UI that can't be used costs, beyond the mods' screens (by installed.Status)
RISKS = {
    Status.TWO_FOLDERS: "Until then, the game's menus may look wrong or break.",
    Status.UNKNOWN: "Until then, Smooth UI itself may not work as intended.",
}
MORE_CAUSES = "And {count} more: see the list below."
MAX_CAUSES = 2          # sentences about causes on a card, the last one perhaps MORE_CAUSES

# In stale mode, an unknown Smooth UI build makes the merger look for an update at once (update_search)
LOOKING, NOTHING_FOUND = "looking", "nothing found"
UPDATE_TOOLS = ("Update Smooth UI Tools", "update")
LOOKING_FOR_UPDATE = "Looking for a newer Smooth UI Tools..."
UPDATE_FOUND = "Smooth UI Tools v{version} is available."
NO_UPDATE = "No newer Smooth UI Tools is out yet."


def status_card(found: Check | None, *, stale: bool, busy: str | None = None, scan_error: str | None = None,
                merged: BuildResult | None = None, merge_error: str | None = None,
                progress: tuple[int, int] | None = None, update: str | None = None,
                update_search: str | None = None) -> Card:
    """What the status card shows, from the window's state. found: the last check (None until one ends);
    stale: whether the launcher opened the window; busy: what runs in a thread ("scan", "build", "remove" or
    "update"); scan_error: why the last check failed; merged and merge_error: the result of the window's
    last merge, or why it failed; progress: the merge's or the update's download (done, total), once known;
    update: a newer Smooth UI Tools version, if one was found; update_search: in stale mode, LOOKING or
    NOTHING_FOUND while or after looking for one (None: not looked for, or the check failed).

    In stale mode, every card has a button that plays, except while merging or removing. There, an unknown
    Smooth UI build (which a newer version of the tools may know) says what the update check found, and
    leads with the update if there is one."""
    if busy == "update":
        fraction = progress[0] / progress[1] if progress and progress[1] else None
        return Card(NEUTRAL, DOWNLOADING_UPDATE.format(version=update), busy=True, progress=fraction)
    card = _card(found, stale, busy, scan_error, merged, merge_error, progress)
    if stale and not busy and found is not None and found.scan.installed.status == Status.UNKNOWN:
        if update:
            return replace(card, sentences=(*card.sentences, UPDATE_FOUND.format(version=update)),
                           buttons=(UPDATE_TOOLS, *card.buttons))
        if update_search == LOOKING:
            return replace(card, sentences=(*card.sentences, LOOKING_FOR_UPDATE), busy=True)
        if update_search == NOTHING_FOUND:
            return replace(card, sentences=(*card.sentences, NO_UPDATE))
    return card


def _card(found, stale, busy, scan_error, merged, merge_error, progress) -> Card:
    if busy == "build":
        return _merging(stale, progress)
    if busy == "remove":
        return Card(NEUTRAL, "Removing the merged patches...", busy=True)
    if busy == "scan" or (found is None and scan_error is None):
        buttons = (("Play without waiting", "play"), DONT_START) if stale else ()
        return Card(NEUTRAL, "Checking your mods...", buttons=buttons, default=False, busy=True)
    if scan_error is not None:
        return Card(ERROR, "Can't read your mods", (_sentence(scan_error),), (PLAY_ANYWAY, DONT_START) if stale else (RESCAN,))
    if merge_error is not None:
        if stale:
            play = PLAY_WITHOUT_MERGING[found.severity] if found and found.severity else "Play without merging"
            buttons = (("Merge again", "merge_and_play"), (play, "play"), DONT_START)
        else:
            buttons = (("Merge again", "merge"), (WRITE_BUG_REPORT, "bug_report"))
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
        if status in ("left out", "failed", "mod failed"):
            causes.append(("failed" if status == "mod failed" else status, file.mod))
        elif file.fixes_not_applied:
            causes.append(("fixes", file.mod))
    notes = []
    if result.check_problems:
        notes.append("Some merged screens don't load as they should: see the notes below.")
    if result.problems or result.installation.written.unknown_files:
        notes.append("See the notes below.")
    headline = ("Merged, but some screens couldn't be merged" if any(file.severity == "error" for file in result.files)
                else "Merged, with problems")
    return Card(WARNING, headline, _sentences(causes, notes), (PLAY_ANYWAY, DONT_START) if stale else (RESCAN,))


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
        if found.scan.conflicts and installed.status in RISKS:
            card = _unusable(installed, stale)
            mods = f"These mods' screens may look wrong too: {_join(found.scan.conflicts)}."
            return replace(card, tone=ERROR, sentences=(*card.sentences, *((mods,) if installed.status == Status.UNKNOWN else ())))
        if found.scan.conflicts:
            sentences = (_sentence(installed.message),
                         f"Until then, these mods' screens may look wrong: {_join(found.scan.conflicts)}.")
            return Card(ERROR, "Smooth UI can't be used with your mods right now", sentences,
                        (PLAY_ANYWAY, DONT_START) if stale else (RESCAN,))
        return _unusable(installed, stale)

    if found.stale:
        files = sorted((file for file in found.states if file.severity is not None), key=lambda file: -file.severity)
        causes = [(_cause(file), _changed_mod(file, found)) for file in files]
        if not found.needs_build:
            # Only another mod at Priority 0 that sorts first: a merge changes nothing
            fix = "Give it a priority above 0 (in your mod manager or mods.settings), then re-scan."
            return Card(WARNING, "Another mod loads before the merged patches", _sentences(causes, [fix]),
                        (PLAY_ANYWAY, DONT_START) if stale else (RESCAN,))
        severity = found.severity
        buttons = ((("Merge and play", "merge_and_play"), (PLAY_WITHOUT_MERGING[severity], "play"), DONT_START) if stale
                   else (("Merge", "merge"), RESCAN))
        return Card(TONES[severity], HEADLINES[severity], _sentences(causes, [COST[severity]] if severity in COST else []),
                    buttons)

    # Up to date: what the merged patches hold, and what they can't
    causes = [(_cause(file), file.winner) for file in found.states
              if file.state == State.UNDETERMINED or (file.state == State.NOT_HELD and file.cause != "unchanged")
              or file.cause == "fixes"]
    patched = [(file.winner, file.path) for file in found.states if file.state == State.UP_TO_DATE]
    buttons = (PLAY,) if stale else (RESCAN, ("Merge again", "merge"))
    if causes:
        headline = ("Some screens are merged without all fixes" if all(cause == "fixes" for cause, _ in causes)
                    else "Some screens can't be merged")
        return Card(WARNING, headline, _sentences(causes), buttons)
    if patched:
        return Card(OK, "Your merged patches are up to date", (f"They hold {_patched(patched)}.",), buttons)
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
        card = Card(WARNING, headline, (_sentence(installed.message), *([RISKS[installed.status]] if installed.status in RISKS else [])),
                    (RESCAN,))
    return replace(card, buttons=(PLAY_ANYWAY, DONT_START)) if stale else card


def row_status(file: FileState) -> tuple[str, str]:
    """A screen's tone (its glyph and color) and short status in the list"""
    state, cause = file.state, file.cause
    if state == State.UP_TO_DATE:
        if cause == "fixes":
            return WARNING, "Merged, fixes missing"
        return OK, "Merged anyway" if cause == "forced" else "Merged"
    if state == State.NOT_HELD:
        return {"unchanged": (OK, "Already smooth"), "failed": (ERROR, "Can't merge"),
                "mod failed": (ERROR, "Mod left out")}.get(cause, (ERROR, "Left out"))
    if state == State.CHANGED:
        return {"merger": (INFO, "Merger updated"), "smoothui": (WARNING, "Smooth UI updated"),
                "load order": (WARNING, "Load order changed")}.get(cause, (WARNING, "Mod updated"))
    return {State.NOTHING_TO_DO: (OK, "Smooth UI's own copy"), State.NOT_BUILT: (WARNING, "Not merged yet"),
            State.NO_LONGER_NEEDED: (WARNING, "Mod removed"), State.DAMAGED: (ERROR, "Damaged"),
            State.OVERRIDDEN: (WARNING, "Another mod loads first"),
            State.UNDETERMINED: (WARNING, "Load order unclear")}[state]


def merging_status(patched: str | None) -> tuple[str, str]:
    """A screen's tone and short status in the list while merging: patched is None until its file is
    patched, then how ("patched", "kept" or "failed"). What goes in is only decided once every file is
    patched (a patched screen may still be left out), so until then it is only "Done"."""
    if patched is None:
        return NEUTRAL, "Waiting"
    return (ERROR, "Can't merge") if patched == "failed" else (NEUTRAL, "Done")


def also_changed(file: FileResult, winner: str | None) -> str | None:
    """Which other mods change the screen, whose copies the game doesn't use (None if no other)"""
    others = [mod.folder for mod in file.shippers if not is_own_folder(mod.folder) and mod.folder != winner]
    return ALSO_CHANGED.format(mods=_join(others), mod=winner) if others and winner else None


def release_notes(notes) -> str:
    """The What's new dialog's text (Markdown, as GitHub's release notes are): the notes ((version, text)) of
    each newer release, newest first, each under a heading"""
    return "\n\n".join(f"# {RELEASE_HEADING.format(version=version)}\n\n{text.strip() or NO_NOTES}"
                       for version, text in notes)


def unchanged_screens(count) -> str:
    """The line below the list's screens, for those no other mod changes"""
    if count == 1:
        return "1 more screen uses Smooth UI's own version, as no other mod changes it"
    return f"{count} more screens use Smooth UI's own version, as no other mod changes them"


def _cause(file: FileState) -> str:
    """The CAUSES key for a file in a check"""
    if file.state == State.OVERRIDDEN:
        return "entry" if file.needs_build else "overridden"
    if file.cause == "mod failed":
        return "failed"
    return file.cause or file.state.value


def _changed_mod(file: FileState, found: Check) -> str | None:
    """The mod a file's cause is about: for a copy that changed, the mod that changed. For a removed mod, the
    one the last merge used; when another mod's copy is used now, the one the last merge used if it no
    longer ships the file (removed or disabled), else the one used now (new, or moved up)."""
    if file.state == State.NO_LONGER_NEEDED:
        return file.previous
    if file.cause == "load order" and file.previous:
        result = found.scan.files.get(file.path)
        if file.previous.lower() not in {mod.folder.lower() for mod in (result.shippers if result else [])}:
            return file.previous
    return file.winner


def _sentences(causes, notes=()) -> tuple[str, ...]:
    """One sentence per cause (see CAUSES), and per kind of cause about mods' copies (see MOD_CAUSES), from
    causes ((key, mod), the most important first), then notes. Only MAX_CAUSES of them: the last one that
    fits then says how many more there are."""
    groups, kinds = {}, {}
    for key, mod in causes:
        if key in MOD_CAUSES:
            if mod:
                kinds.setdefault(MOD_CAUSES[key], {}).setdefault(mod.lower(), mod)
        else:
            groups.setdefault((key, mod if "{mod}" in CAUSES[key] else None), None)
    sentences = [CAUSES[key].format(mod=mod) for key, mod in groups]
    for kind, mods in kinds.items():
        names = sorted(mods.values(), key=str.lower)
        count = f"{NUMBERS[len(names)] if len(names) < len(NUMBERS) else len(names)} {'mod' if len(names) == 1 else 'mods'}"
        sentences.append(MODS[kind][len(names) > 1].format(count=count, mods=_join(names)))
    if len(sentences) > MAX_CAUSES:
        sentences[MAX_CAUSES - 1:] = [MORE_CAUSES.format(count=len(sentences) - MAX_CAUSES + 1)]
    return (*sentences, *notes)


def _patched(files) -> str:
    """E.g. "10 screens from 5 mods", for files ((mod, path))"""
    return f"{_count(len(files), 'screen')} from {_count(len({mod.lower() for mod, _ in files}), 'mod')}"


def _join(names) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _count(number, noun) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def capitalized(text) -> str:
    """text with its first letter in upper case, unless it starts with a mod's folder name (the game loads only
    folders whose names start with "mod", e.g. modSortEverything)"""
    return text if text.startswith("mod") else text[:1].upper() + text[1:]


def _sentence(text) -> str:
    """A library message (e.g. "can't read ...: ...") as a sentence"""
    text = capitalized(text)
    return text if text.endswith((".", "!", "?")) else f"{text}."
