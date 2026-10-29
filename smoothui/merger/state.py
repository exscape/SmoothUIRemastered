"""Whether the installed package is up to date: for each file the installed Smooth UI build patches, what the
package holds, compared with what a build would put in it now (package_state), and from that, whether the
merger should open before the game starts (check: the launcher, merge.py --scan and the merger's window
all decide by it).

Read-only, from a scan, the package's manifest and its bundle's table of contents; nothing is patched. So
it tells whether the package was built from the copies the game would use now, not whether a build
would patch them cleanly. Light: the launcher imports it, so it must not import the patcher.

A package that would hold no files is removed, and its manifest kept in a record in the merger's data
folder instead (see package.py), which counts while there is no package.

A group of files (e.g. the HUD; see policy.py) only works whole: while the package holds a group, anything
that would leave the game with a mix of patched and unpatched copies of its files is as bad as a wrong copy
(Severity.HIGH). A group the user
turned off (scan.Scan.groups_off) is fine while the package doesn't hold it; one turned on or off since the
last build needs a build (Severity.MEDIUM: either way, the game shows the whole group one way). The same goes
for a screen the user chose not to merge (scan.Scan.screens_off).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from pathlib import Path

from .. import mod_reviews
from ..common import SmoothUIError
from ..files import read_file
from ..paths import DATA_DIR
from ..witcher3.mod_bundles import list_bundles, read_bundle_tocs
from ..witcher3.mods_settings import ModSettings
from ..witcher3.source_file import GAME_FILES
from . import tools_version
from .installed import is_smoothui_folder
from .package import PACKAGE_MANIFEST, PACKAGE_MANIFEST_FORMAT, record_path, same_copy
from .scan import (
    COMPAT_PATCH,
    PACKAGE_FOLDER,
    Scan,
    entry_needed,
    is_package_folder,
    scan,
)
from .screens import group_name, screen_name


class State(Enum):
    NOTHING_TO_DO = "nothing to do"     # no other mod ships it, and the package doesn't hold it
    UNDETERMINED = "undetermined"       # the load order doesn't decide which copy wins (see the scan's problems)
    UP_TO_DATE = "up to date"           # the package holds the winning copy, patched, and the game loads it
    NOT_HELD = "not held"               # the last build didn't put it in the package (its status says why), and
                                        # the winning copy is still the one it decided on
    NOT_BUILT = "not built"             # another mod ships it, and the package has no record of it
    CHANGED = "changed"                 # the package was built from another copy (the mod changed, or another
                                        # mod wins now), for another Smooth UI build, or by another version
                                        # of the merger
    NO_LONGER_NEEDED = "no longer needed"   # the package holds it, but no other mod ships it now
    DAMAGED = "damaged"                 # the package's copy isn't the one built, or the package is incomplete
    OVERRIDDEN = "overridden"           # the package holds it, but the game loads another mod's copy, which
                                        # comes before the package in the load order

    @property
    def needs_build(self) -> bool:
        """Whether building the package again always changes it. (OVERRIDDEN is about the load order, which a
        build only fixes if the package's own mods.settings entry is the cause: see FileState.needs_build.)"""
        return self in (State.NOT_BUILT, State.CHANGED, State.NO_LONGER_NEEDED, State.DAMAGED)


class Severity(IntEnum):
    """What playing without building the package again costs"""
    LOW = 1         # nothing: only the merger's version changed since the build
    MEDIUM = 2      # a mod's copy that the game loads isn't patched: it runs at its original frame rate
    HIGH = 3        # the package overrides a file with a copy that doesn't fit what is installed now (a mod
                    # updated or removed, another mod's copy wins, another Smooth UI build, a damaged
                    # package): that screen may look wrong, or that mod may not work at all


@dataclass(frozen=True, slots=True)
class FileState:
    path: str                   # profile key
    state: State
    message: str                # for the user
    winner: str | None = None   # the mod whose copy a build would patch (see scan.FileResult.winner); for a
                                # screen the user chose not to merge, the mod whose copy that is about
    severity: Severity | None = None    # set for the files that open the merger at launch (see Check)
    needs_build: bool = False   # whether building the package again would change it (see State.needs_build)
    cause: str | None = None    # why, in a word, for the window to group files by: for CHANGED, "smoothui" or
                                # "merger" (updated since the build), "updated" (the mod's copy changed),
                                # "load order" (another mod's copy wins now) or "setting" (its group was
                                # turned off since the build); for NOT_BUILT, "setting" if its group was turned on
                                # since the build; for NOT_HELD, the status the manifest records (see
                                # WHY), "turned off" (its group is), "by choice" (the user
                                # chose not to merge it, or a file of its group; also CHANGED's and
                                # NOT_BUILT's cause once that changed since the build), or "by rule" (our
                                # dont-merge line does, see mod_reviews.py: then reason is its reason; the
                                # same causes for CHANGED and NOT_BUILT); for UP_TO_DATE, "forced" if
                                # the user chose to patch it anyway
    previous: str | None = None     # CHANGED (but for "smoothui", "merger") and NO_LONGER_NEEDED: the mod whose
                                    # copy the last merge used
    fixes: tuple[str, ...] = ()     # UP_TO_DATE: the fixes the merged copy lacks (a fix that can't apply)
    group: str | None = None        # the group it is in, if any (see build/profile.py's Group)
    reason: str | None = None       # NOT_HELD "left out", and UP_TO_DATE "forced": why its rescale has problems,
                                    # in a word (see WHY), if the build recorded it; "by rule": the rule's


@dataclass(frozen=True, slots=True)
class Check:
    """What check found"""
    scan: Scan
    states: list[FileState]         # see package_state
    package_without_smoothui: bool  # a package is installed, but the installed Smooth UI can't be used
                                    # (scan.installed.message says why): the package overrides files with
                                    # copies patched for another Smooth UI build, or for none

    @property
    def stale(self) -> bool:
        """Whether the launcher opens the merger before the game starts. The scan's problems never do by
        themselves: a build can't fix them (the merger lists them when it opens anyway); scan.conflicts do."""
        return self.severity is not None

    @property
    def severity(self) -> Severity | None:
        if self.package_without_smoothui or self.scan.conflicts:
            return Severity.HIGH
        return max((state.severity for state in self.states if state.severity is not None), default=None)

    @property
    def needs_build(self) -> list[FileState]:
        """The files that building the package again would change"""
        return [state for state in self.states if state.needs_build]


def check(game_path: Path, mods_settings: Path, *, data_dir: Path | None = DATA_DIR, **scan_options) -> Check:
    """Scan (scan_options go to scan.scan, for tests), and compare with the package (see package_state;
    data_dir as there). Fails (SmoothUIError) only if the scan does."""
    result = scan(game_path, mods_settings, **scan_options)
    package_without_smoothui = not result.installed.usable and _has_package(result)
    return Check(result, package_state(result, data_dir), package_without_smoothui)


def _has_package(result: Scan) -> bool:
    """Whether the game loads a package (an enabled package folder with a bundle)"""
    folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), None)
    if folder is None:
        return False
    try:
        return bool(list_bundles(result.mods_dir / folder / "content"))
    except OSError:
        return False    # no content folder: nothing for the game to load


# Why a copy isn't merged, for the user, after the copy (e.g. "modA's copy uses code ..."): by the status a
# build gives it (policy.FileStatus's values), and for "left out", by the reason for it (policy.PackageFile.reason;
# records of older mergers have none). Here, as policy.py isn't imported: the launch check doesn't need the
# patcher. The technical details are for the log only.
WHY = {
    "code": "uses code Smooth UI hasn't been checked with yet",
    "changes": "changes the screen in a way Smooth UI doesn't expect",
    "left out": "isn't fully supported by Smooth UI",
    "known broken": "is known not to work with Smooth UI",
}


def not_merged(status: str, copy: str, mod: str, *, reason: str | None = None, group: str | None = None,
               kept_out_by: str | None = None) -> str:
    """Why a copy (e.g. "modA's copy") of mod isn't merged, for the user: by its status (as for WHY, or
    "failed", "mod failed", "group left out") and reason. A group's file left out with it: kept_out_by, why
    the group is (e.g. "hud_buffs can't be merged")."""
    if status == "failed":
        return f"{copy} makes the merger fail, so none of {mod}'s screens are merged."
    if status == "mod failed":
        return f"another of {mod}'s screens makes the merger fail, so none of them are merged."
    if status == "group left out":
        return (f"not merged, as the {group_name(group)} is merged whole or not at all"
                + (f", and {kept_out_by}." if kept_out_by else "."))
    return f"{copy} {WHY.get(reason) or WHY.get(status, status)}, so it isn't merged."


def left_out_by_rule(choice, group: str | None = None) -> str:
    """Why a copy of a screen that our own rule leaves out (a dont-merge line; choice: its ScreenOff) isn't
    merged, for the user: by the rule's reason. A group's file: said of the group, the same for each of its
    files."""
    copy = f"{choice.mod}'s copy"
    if choice.reason == mod_reviews.ALREADY_SMOOTH:
        clause = "already smooth"
    elif choice.reason == mod_reviews.KNOWN_BROKEN:
        clause = "known not to work with Smooth UI"
    else:   # a reason from newer tools
        clause = "left out by Smooth UI for this mod"
    if group:
        # (Not naming the file: each of the group's files that a rule leaves out says the same, so that they are told as one)
        return (f"not merged, as the {group_name(group)} is merged whole or not at all, and {choice.mod}'s "
                f"copy of it is {clause}.")
    if choice.reason == mod_reviews.ALREADY_SMOOTH:
        return f"{copy} is already smooth, so Smooth UI leaves it as it is."
    return f"{copy} is {clause}, so it isn't merged."


def merged_anyway(copy: str, reason: str | None, since: str = "") -> str:
    """What a copy (e.g. "modA's copy") merged anyway, as the user chose (since: e.g. " on <date>"), means,
    by its reason (see WHY): code that isn't checked often works, so that is said gently"""
    if reason == "code":
        return (f"merged anyway, as you chose{since}: {copy} {WHY['code']}. If something runs too fast or looks wrong, "
                "revert it")
    return f"merged anyway, as you chose{since}: {copy} {WHY.get(reason) or WHY['left out']}, so it may look or behave wrong in places"


def original_frame_rate(group: str | None = None) -> str:
    """What the game shows for a file that isn't merged, when that is its own copy (the game's or its mod's):
    said of the whole group for a group's file, so that its files say the same"""
    return f"The {group_name(group)} runs at its original frame rate." if group else "It runs at its original frame rate."


def package_state(result: Scan, data_dir: Path | None = DATA_DIR) -> list[FileState]:
    """A FileState for each file in the scan, by path (none if the installed Smooth UI can't be used).
    data_dir: the merger's data folder, with the records of packages that hold no files (None: none)."""
    if not result.files:
        return []
    folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), PACKAGE_FOLDER)
    package_dir = result.mods_dir / folder
    manifest, manifest_problem = _read_manifest(package_dir / PACKAGE_MANIFEST)
    toc, toc_problem = _read_toc(package_dir, set(result.files))
    if manifest is None and manifest_problem is None and not toc and toc_problem is None and data_dir is not None:
        manifest, manifest_problem = _read_manifest(record_path(data_dir, result.mods_dir))
    installed = result.installed
    built_for = (manifest or {}).get("smoothui", {})
    built_by = (manifest or {}).get("tools_version")
    # Only the merger's version changed: nothing the game shows is wrong (severity LOW)
    if built_for.get("version") != installed.version or built_for.get("release_created_at") != installed.manifest.get("created_at"):
        rebuilt, rebuilt_text = "smoothui", "Smooth UI was updated"
    elif built_by != tools_version():
        rebuilt, rebuilt_text = "merger", "the merger was updated"
    else:
        rebuilt = rebuilt_text = None
    merger_only = rebuilt == "merger"
    # New reviews of mods' code (downloaded since the build) may let a screen that was left out be merged
    reviews = result.reviews
    reviews_changed = (manifest or {}).get("mod_reviews_sha256") != reviews.sha256

    def still_broken(record):
        """Whether the reviews say a copy known not to work still is (see policy.FileStatus.KNOWN_BROKEN)"""
        return any(reviews.leaves_out(record["mod"], review) for review in record.get("known_broken", ()))

    # Whether the package's own mods.settings entry is as a build writes it (see mods_settings.write_entry)
    # (Priority 0, or any entry that a build wouldn't change because the package loads first anyway)
    own_entry = any(is_package_folder(mod.folder) and mod.settings is not None
                    and (mod.settings == ModSettings(True, 0) or not entry_needed(result.order, mod.settings))
                    for mod in result.order)
    held = (manifest or {}).get("files", {})
    not_held = (manifest or {}).get("not_held", {})
    groups = result.group_of
    was_off = set((manifest or {}).get("groups_off", ()))
    screens_were_off = set((manifest or {}).get("screens_off", ()))
    rules_were = (manifest or {}).get("screens_off_by_rule", {})     # {key: reason} of those our rules left out

    states = []
    for key in sorted(result.files):
        file = result.files[key]
        winner = file.winner
        origin = winner.origin if winner else None

        def state(kind, message, severity=None, needs_build=None, cause=None, previous=None, fixes=(), key=key,
                  origin=origin, reason=None):
            needs_build = kind.needs_build if needs_build is None else needs_build
            states.append(FileState(key, kind, message, origin, severity, needs_build, cause, previous, fixes,
                                    groups.get(key), reason))

        if file.undetermined:
            state(State.UNDETERMINED, "the load order doesn't decide which mod's copy the game uses (see the problems)")
        elif toc_problem and (key in held or (manifest is None and winner is not None)):
            state(State.DAMAGED, f"the merged patches can't be used ({toc_problem}): merge again", Severity.HIGH)
        elif groups.get(key) in result.groups_off:
            name = group_name(groups[key])
            shows = _shows(file, origin, groups[key])
            if key in toc:
                state(State.CHANGED, f"merging the {name} is turned off in the settings, but the merged patches hold "
                      f"it: merge again. {shows}", Severity.MEDIUM, needs_build=True, cause="setting")
            else:
                state(State.NOT_HELD, f"merging the {name} is turned off in the settings. {shows}", cause="turned off")
        elif key in result.screens_off and result.screens_off[key].reason is not None:
            choice = result.screens_off[key]
            text = left_out_by_rule(choice, groups.get(key))
            shows = _shows(file, choice.mod, groups.get(key))
            if key in toc:
                state(State.CHANGED, f"{text} But the merged patches hold it: merge again. {shows}", Severity.MEDIUM, needs_build=True, cause="by rule", reason=choice.reason, origin=choice.mod)
            else:
                state(State.NOT_HELD, f"{text} {shows}", cause="by rule", reason=choice.reason, origin=choice.mod)
        elif key in result.screens_off:
            choice = result.screens_off[key]
            shows = _shows(file, choice.mod, groups.get(key))
            since = f" (since {choice.since})" if choice.since else ""
            # The same for each of a group's files (the chosen one too), so that they are told as one
            what = (f"merging the {group_name(groups[key])} is turned off, as merging {choice.mod}'s copy of "
                    f"{screen_name(choice.path)} is{since}" if key in groups else
                    f"merging {choice.mod}'s copy is turned off{since}")
            if key in toc:
                state(State.CHANGED, f"{what}, but the merged patches hold it: merge again. {shows}",
                      Severity.MEDIUM, needs_build=True, cause="by choice", origin=choice.mod)
            else:
                state(State.NOT_HELD, f"{what}. {shows}", cause="by choice", origin=choice.mod)
        elif key in toc:
            record = held.get(key)
            entry = toc[key][1]
            if record is None:
                why = manifest_problem or "its manifest doesn't list this screen"
                state(State.DAMAGED, f"the merged patches are damaged ({why}): merge again", Severity.HIGH)
            elif winner is None:
                after = file.loaded_without_package
                state(State.NO_LONGER_NEEDED, f"{record['mod']} no longer changes it: merge again (the game then uses "
                      f"{_copy_name(after.folder) if after else 'no copy'})", Severity.HIGH, previous=record["mod"])
            elif (entry.size, entry.crc) != (record.get("output_size"), record.get("output_crc32")):
                state(State.DAMAGED, "the merged patches are damaged: merge again", Severity.HIGH)
            elif rebuilt:
                state(State.CHANGED, f"{rebuilt_text}: merge again", Severity.LOW if merger_only else Severity.HIGH,
                      cause=rebuilt)
            elif not same_copy(record, winner):
                cause, text = _changed(record, winner)
                state(State.CHANGED, f"{text}: merge again", Severity.HIGH, cause=cause, previous=record.get("mod"))
            elif not (file.loaded and is_package_folder(file.loaded.folder)):
                loaded = file.loaded.folder if file.loaded else "another mod"
                if own_entry:
                    state(State.OVERRIDDEN, f"the game uses {loaded}'s copy: it has Priority 0 too, and its name sorts "
                          "first. Give it a priority above 0 (in mods.settings or your mod manager), then scan again",
                          Severity.MEDIUM)
                else:
                    state(State.OVERRIDDEN, f"the game uses {loaded}'s copy, as the merged patches' mods.settings entry "
                          "is missing or changed: merge again", Severity.MEDIUM, needs_build=True)
            elif record.get("forced"):
                since = f" on {record['forced_since']}" if record.get("forced_since") else ""
                fixes = tuple(record.get("fixes_not_applied") or ())
                # (The window adds a colon, then the fixes)
                text = merged_anyway(_copy_of(origin), record.get("reason"), since) + (
                    ". It is merged without some fixes" if fixes else ".")
                state(State.UP_TO_DATE, text, cause="forced", fixes=fixes, reason=record.get("reason"))
            else:
                fixes = tuple(record.get("fixes_not_applied") or ())
                state(State.UP_TO_DATE, f"merged ({_copy_of(origin)})" + (", without some fixes" if fixes else ""),
                      cause="fixes" if fixes else None, fixes=fixes)
        elif key in held:
            state(State.DAMAGED, "the merged patches are damaged (their manifest lists it, their bundle doesn't): "
                  "merge again", Severity.HIGH)
        elif winner is None:
            state(State.NOTHING_TO_DO, f"no other mod changes it: the game uses {_copy_name(file.loaded.folder)}"
                  if file.loaded else "no mod has it")
        elif key in not_held:
            # The package doesn't hold it, so the game shows the winning copy, unpatched
            record = not_held[key]
            if rebuilt:
                state(State.CHANGED, f"{rebuilt_text}: merge again", Severity.LOW if merger_only else Severity.MEDIUM,
                      cause=rebuilt)
            elif not same_copy(record, winner):
                cause, text = _changed(record, winner)
                state(State.CHANGED, f"{text}: merge again", Severity.MEDIUM, cause=cause, previous=record.get("mod"))
            elif reviews_changed and (record["status"] == "left out"
                                      or (record["status"] == "known broken" and not still_broken(record))):
                state(State.CHANGED, "the merger was updated since this screen was left out: merge again, as it may "
                      "merge it now", Severity.MEDIUM, cause="merger")
            elif record["status"] == "unchanged":
                state(State.NOT_HELD, f"{_copy_of(origin)}: already smooth, nothing to merge", cause="unchanged")
            else:
                # Short, for the user: the technical reasons are in the merge's log
                status, group = record["status"], groups.get(key)
                kept_out_by = "; ".join(record.get("reasons") or ()) if status == "group left out" else None
                text = not_merged(status, _copy_of(origin), origin, reason=record.get("reason"), group=group,
                                  kept_out_by=kept_out_by)
                # The same for each of a group's files left out with it, so that they are told as one
                shows = _shows(file, origin, group if status == "group left out" else None)
                state(State.NOT_HELD, f"{text} {shows}", cause=status, reason=record.get("reason"))
        elif manifest is None and manifest_problem is None:
            state(State.NOT_BUILT, f"{_copy_of(origin)} isn't merged yet", Severity.MEDIUM)
        elif key in rules_were:
            what = f"the {group_name(groups[key])}" if key in groups else _copy_of(origin)
            state(State.NOT_BUILT, f"Smooth UI no longer leaves {what} out: merge again", Severity.MEDIUM,
                  cause="by rule")
        elif key in screens_were_off:
            what = f"the {group_name(groups[key])}" if key in groups else _copy_of(origin)
            state(State.NOT_BUILT, f"merging {what} was turned on again since the last merge: merge again",
                  Severity.MEDIUM, cause="by choice")
        elif groups.get(key) in was_off:
            state(State.NOT_BUILT, f"merging the {group_name(groups[key])} was turned on in the settings since the last "
                  "merge: merge again", Severity.MEDIUM, cause="setting")
        else:
            state(State.NOT_BUILT, f"{_copy_of(origin)} is new since the last merge: merge again", Severity.MEDIUM)
    return _whole_groups(result, states)


def _whole_groups(result: Scan, states: list[FileState]) -> list[FileState]:
    """states, for a package holding a group (see the module's doc): while the game loads some of the group's
    files from the package, the others (each not patched, e.g. a file a mod added to the group since) leave
    it mixed, which is as bad as a wrong copy. (If the game loads none of them from the package, e.g. its
    mods.settings entry is disabled, the whole group is unpatched: not mixed.)"""
    groups = result.group_of
    loaded_groups = {groups[key] for key, file in result.files.items()
                     if key in groups and file.loaded and is_package_folder(file.loaded.folder)}
    result_states = []
    for file in states:
        group = file.group
        # (Turned off or on, by the setting or the user's choice, is the whole group: not mixed)
        if group in loaded_groups and file.severity == Severity.MEDIUM and file.cause not in ("setting", "by choice", "by rule"):
            file = replace(file, severity=Severity.HIGH,
                           message=f"{file.message} (until then, the {group_name(group)} is part patched, part not: "
                           "its animations play at the wrong speeds)")
        result_states.append(file)
    return result_states


def changed_since_off(result: Scan) -> dict[str, list[str]]:
    """The files the user chose not to merge (see Scan.screens_off) for which something changed since that
    may let them merge now: {profile key: what changed, e.g. ["modA", "Smooth UI Tools"]}. Only the chosen
    files, not the rest of their groups, and only choices that recorded what they were made with."""
    found = {}
    for key, choice in sorted(result.screens_off.items()):
        copy = result.off_copies.get(key)
        if choice.path != key or copy is None or choice.size is None:
            continue
        changed = []
        if (copy.size, copy.toc_entry.crc) != (choice.size, choice.crc32):
            changed.append(choice.mod)
        if choice.reviews != result.reviews.sha256 or choice.tools != tools_version():
            changed.append("Smooth UI Tools")
        if changed:
            found[key] = changed
    return found


def carried_force(result: Scan) -> tuple[list[str], list[str]]:
    """The files the package holds because the last build forced them (see policy.decide), to force again
    in the next build: those whose copy is still the one it forced. And a note for each other one, which
    isn't forced, so that the user decides again."""
    folder = next((mod.folder for mod in result.order if is_package_folder(mod.folder)), PACKAGE_FOLDER)
    manifest, _ = _read_manifest(result.mods_dir / folder / PACKAGE_MANIFEST)
    force, notes = [], []
    for key, record in sorted((manifest or {}).get("files", {}).items()):
        if not record.get("forced"):
            continue
        file = result.files.get(key)
        if file is not None and file.winner is not None and same_copy(record, file.winner):
            force.append(key)
        else:
            notes.append(f"{screen_name(key)}: you chose to merge it anyway, but its copy changed since, so that "
                         "choice no longer applies")
    return force, notes


def _read_manifest(path):
    """(the package manifest at path, or None; why it can't be used, or None if there is none)"""
    try:
        data = read_file(path)
    except SmoothUIError as e:
        return None, str(e)
    if data is None:
        return None, None
    try:
        manifest = json.loads(data)
    except ValueError:
        return None, "its manifest can't be read"
    if not isinstance(manifest, dict) or manifest.get("format") != PACKAGE_MANIFEST_FORMAT:
        return None, "its manifest is from another version of the merger"
    return manifest, None


def _read_toc(package_dir, keys):
    """({profile key: (bundle, TocEntry)} for keys in the package's bundles; why they can't be read, or None)"""
    content = package_dir / "content"
    try:
        bundles = list_bundles(content)
        names = {entry.name.lower() for entry in os.scandir(content)}
    except (FileNotFoundError, NotADirectoryError):
        return {}, None
    except OSError as e:
        return {}, str(e)
    if bundles and "metadata.store" not in names:
        return {}, "it has no metadata.store, so the game doesn't load it"
    try:
        return read_bundle_tocs(content, keys), None
    except SmoothUIError as e:
        return {}, str(e)


def _changed(record, winner):
    """(FileState.cause, the message) for a record about another copy than the winner's"""
    if str(record.get("mod", "")).lower() != winner.origin.lower():
        return "load order", f"{_copy_of(winner.origin)} is used now, not {_copy_of(record.get('mod'))}"
    return "updated", "the game was updated" if winner.origin == GAME_FILES else f"{winner.origin} was updated"


def _copy_of(origin):
    """A copy, by where it came from (a mod's folder, or GAME_FILES), for messages"""
    return "the game's own copy" if origin == GAME_FILES else f"{origin}'s copy"


def _shows(file, origin, group=None):
    """What the game shows for a file while the package doesn't hold it, as a sentence: its own copy (the
    game's or its mod's; origin: that mod, if known), said of group if given (see original_frame_rate), or
    another copy that comes first (Smooth UI's own, or a compat patch's)"""
    shown = file.loaded_without_package
    if shown and shown.folder != origin and (is_smoothui_folder(shown.folder) or COMPAT_PATCH.fullmatch(shown.folder)):
        return f"The game uses {_copy_name(shown.folder)} instead."
    return original_frame_rate(group)


def _copy_name(folder):
    if is_smoothui_folder(folder):
        return "Smooth UI's own copy"
    if COMPAT_PATCH.fullmatch(folder):
        return f"the copy of {folder} (a Smooth UI compat patch)"
    return f"{folder}'s copy"
