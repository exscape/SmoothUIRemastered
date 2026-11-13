"""Which patched files go into the package, and what to tell the user about each file.

- A file that fails to patch (any exception) is left out, and this can't be overridden. The mod's other
  files aren't affected: a mix of patched and unpatched copies of one mod's screens all have the mod's changes.
- A fix that can't apply keeps nothing out: the file is used without the fix, and the user is told what
  that means (the fix's CONSEQUENCE).
- Other problems with a rescale (a setting that wasn't followed or no longer matches, an unreviewed
  warning) leave out that file only, unless the user forces it (force).
- A warning that, for that mod, means its copy doesn't work with Smooth UI (mod_reviews.py's leave-out
  lines) leaves out the file the same way, but as known not to work: what Smooth UI intends, not a problem
  to report.
- A group's files (files that only work together, e.g. the HUD) are all in the package, or none are: if one
  isn't (for any of the reasons above, or because there is no copy of it to patch), the whole group is left
  out. Releases don't ship such groups, so the game then shows its own (or the mods') unpatched copies of
  all of them: never a mix. A file a mod adds to a group's folder is one of the group's (see
  scan.Scan.group_of).

A file left out isn't patched, so the game shows the mod's own copy at its original frame rate, or a compat
patch's that comes before it in the load order. Where Smooth UI's own copy comes first, the package holds the
mod's copy as it is, so that the mod's changes still show (see builder.as_is_copies; a group's files are never
Smooth UI's). Only a copy whose bundle is damaged can't be held: then the game shows Smooth UI's.

decide() is cheap and pure: a GUI can call it again with other choices without patching again.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import NamedTuple

from ..build.release import ReleasePatch
from ..common import fail
from ..manifests import FixOutcome
from ..patcher.fixes import get_fix
from ..patcher.patcher_core import FailedFile, KeptFile, PatchedFile
from ..witcher3.source_file import GAME_FILES, SourceFile
from .installed import is_smoothui_folder
from .scan import COMPAT_PATCH, Scan
from .screens import file_name, screen_label
from .state import merged_anyway, not_merged, original_frame_rate, played_slowly
from .status import FileStatus, Level, Reason

# A copy kept at a rate above the target counts as playing slowly below this speed (e.g. a 120 fps copy below
# 102 fps); a little slower than made for is hard to notice
SLOW_BELOW = 0.85


class IssueKind(Enum):
    RULE = "rule"           # a setting that wasn't followed, or that matched in the release but matches nothing
                            # in this mod's copy (the mod changed what it was for)
    WARNING = "warning"     # a patcher warning that hasn't been reviewed
    KNOWN_BROKEN = "known broken"   # a warning that means the mod's copy is known not to work
    FIX = "fix"             # a fix that can't apply to this mod's copy


@dataclass(frozen=True, slots=True)
class Issue:
    kind: IssueKind
    message: str
    fix: str | None = None      # for FIX: the fix's name
    review: str | None = None   # for WARNING and KNOWN_BROKEN: the review that would cover the warning

    @property
    def keeps_out(self) -> bool:
        """Whether it keeps the file out of the package, unless forced"""
        return self.kind is not IssueKind.FIX


class AsIs(NamedTuple):
    """A mod's copy that isn't merged, which the package holds unpatched, as the game would load Smooth UI's
    copy otherwise (see builder.as_is_copies)"""
    path: str               # profile key
    source: SourceFile
    entry: object | None    # the copy as a BundleEntry; None if it can't be read: then the package can't hold it
    status: FileStatus      # its status in the manifest (see package_manifest)


@dataclass(frozen=True, slots=True)
class PackageFile:
    """One file the package could hold: a file Smooth UI patches, whose winning copy is another mod's"""
    path: str                       # profile key
    source: SourceFile              # the copy that was patched
    status: FileStatus
    issues: list[Issue] = field(default_factory=list)
    result: PatchedFile | KeptFile | None = None    # patch_files' result for it, unless it failed
    error: str | None = None        # FAILED: why; GROUP_LEFT_OUT: why the group can't be merged whole
    fallback: str | None = None     # the mod whose copy the game loads unless the package holds this file
                                    # (its own mod's, once the package holds its copy as it is: see build_package)
    group: str | None = None        # the group it is in, if any (see build/profile.py's Group)

    @property
    def mod(self) -> str:
        return self.source.origin

    @property
    def forced(self) -> bool:
        return self.status is FileStatus.FORCED

    @property
    def entry(self):
        """The patched file, as a BundleEntry (for a file the package holds)"""
        assert isinstance(self.result, PatchedFile)
        return self.result.entry

    @property
    def fps(self) -> float | None:
        """The frame rate its copy keeps, if it isn't rescaled (UNCHANGED, or only fixed); else None"""
        if self.status not in (FileStatus.UNCHANGED, FileStatus.PATCHED, FileStatus.FORCED):
            return None
        assert self.result is not None
        report = self.result.report
        return report.old_fps if report.new_fps is None else None

    @property
    def speed(self) -> float | None:
        """For a copy that keeps its frame rate (see fps): the speed it plays at, if its animations play
        noticeably slower than made for (below SLOW_BELOW, and it has timeline animations); else None"""
        if self.fps is None:
            return None
        assert self.result is not None
        report = self.result.report
        speed = report.target_fps / report.old_fps
        return speed if report.animated and speed < SLOW_BELOW else None

    @property
    def known_broken(self) -> list[str]:
        """The reviews of the warnings that mean its copy is known not to work"""
        return [issue.review for issue in self.issues if issue.kind is IssueKind.KNOWN_BROKEN and issue.review is not None]

    @property
    def fixes_not_applied(self) -> list[str]:
        return [issue.fix for issue in self.issues if issue.kind is IssueKind.FIX and issue.fix is not None]

    @property
    def severity(self) -> Level:
        """OK (as Smooth UI would patch it, or patched anyway as the user chose, or known not to work), WARNING
        (without some fixes) or ERROR (not in the package, although it needs to be)"""
        if self.status is FileStatus.KNOWN_BROKEN:
            return Level.OK
        if self.status in (FileStatus.PATCHED, FileStatus.UNCHANGED, FileStatus.FORCED) and not self.fixes_not_applied:
            return Level.OK
        # A group left out whole is consistent (the file that keeps it out is the error)
        if self.status.included or self.status in (FileStatus.UNCHANGED, FileStatus.GROUP_LEFT_OUT):
            return Level.WARNING
        return Level.ERROR

    @property
    def reason(self) -> Reason | None:
        """Why its rescale has problems, in a word for the user (see state.WHY); None if none"""
        kinds = {issue.kind for issue in self.issues}
        for kind, reason in ((IssueKind.KNOWN_BROKEN, Reason.KNOWN_BROKEN), (IssueKind.WARNING, Reason.CODE),
                             (IssueKind.RULE, Reason.CHANGES)):
            if kind in kinds:
                return reason
        return None

    @property
    def to_report(self) -> bool:
        """Whether it is worth a bug report: left out for a problem (not known not to work), or failed"""
        return self.status in (FileStatus.LEFT_OUT, FileStatus.FAILED)

    def explain(self, label: str | None = None) -> str:
        """What happened to the file, for the user, in short: if it isn't patched as Smooth UI would, why (in
        plain words), and what the game shows instead. The first line is the summary, after label (default: its
        screen_label); the others are indented: the fixes it lacks, and the technical details. A group's files
        left out with it say the same, so that they can be told as one (see screens.by_group)."""
        status, copy = self.status, self._copy()
        if status is FileStatus.PATCHED:
            summary = f"merged ({copy})" + (", without some fixes:" if self.issues else "")
        elif status is FileStatus.UNCHANGED:
            summary = f"{copy[0].upper() + copy[1:] if self.mod == GAME_FILES else copy} runs at {self.fps:g} fps: used as it is" + (", without some fixes:" if self.issues else "")
        elif status is FileStatus.FORCED:
            summary = merged_anyway(copy, self.reason) + (". Without some fixes:" if self.fixes_not_applied else ".")
        elif status is FileStatus.GROUP_LEFT_OUT:
            summary = f"{not_merged(status, copy, group=self.group, kept_out_by=self.error)} {self._runs()}"
        else:
            reason = self.reason if status is FileStatus.LEFT_OUT else None
            summary = f"{not_merged(status, copy, reason=reason)} {self._runs()}"
        lines = self._fix_lines() if status.included or status is FileStatus.UNCHANGED else []
        if self.speed is not None and self.result is not None:
            lines.append(played_slowly(self.result.report.target_fps, self.speed))
        if details := self._details():
            lines.append(f"Details: {details}")
        return "\n  ".join([f"{screen_label(self.path) if label is None else label}: {summary}", *lines])

    def _details(self):
        """The technical reasons, for the log: the warnings' reviews (see mod_reviews.py), the settings'
        problems, the failure, the fixes that can't apply; none for a file left out with its group"""
        if self.status is FileStatus.FAILED:
            return self.error
        if self.status is FileStatus.GROUP_LEFT_OUT:
            return None
        shown = self.issues
        if self.status is FileStatus.KNOWN_BROKEN:
            shown = [issue for issue in shown if issue.kind is IssueKind.KNOWN_BROKEN]
        elif self.status in (FileStatus.PATCHED, FileStatus.UNCHANGED):
            shown = [issue for issue in shown if issue.kind is IssueKind.FIX]
        return "; ".join(issue.review if issue.review else f"{issue.fix}: {issue.message}" if issue.fix else issue.message
                         for issue in shown) or None

    def _fix_lines(self):
        return ["- " + get_fix(issue.fix).missing_text()
                for issue in self.issues if issue.kind is IssueKind.FIX and issue.fix is not None]

    def _copy(self):
        """The copy that was patched, for the user: the game's copy, or <mod>'s copy"""
        return "the game's copy" if self.mod == GAME_FILES else f"{self.mod}'s copy"

    def _runs(self):
        """What the game shows instead, as a sentence: the file's own copy (said of its whole group, if it is
        left out with it), or another"""
        if self.fallback is None or self.fallback == self.mod:
            return original_frame_rate(self.group if self.status is FileStatus.GROUP_LEFT_OUT else None)
        return f"The game uses {self._fallback()} instead."

    def _fallback(self):
        """The copy the game shows unless the package holds the file, when it isn't the file's own"""
        assert self.fallback is not None    # see _runs
        if is_smoothui_folder(self.fallback):
            if self.mod == GAME_FILES:
                return "Smooth UI's own copy"
            return f"Smooth UI's own copy, without {self.mod}'s changes (it comes first in the load order)"
        if COMPAT_PATCH.fullmatch(self.fallback):
            return f"the copy of {self.fallback} (a Smooth UI compat patch)"
        return f"{self.fallback}'s copy (it comes before {self.mod}'s in the load order)"


def decide(scan: Scan, patch: ReleasePatch, *, force=()) -> list[PackageFile]:
    """A PackageFile for each file patch holds (patch_as_release's result for the scan's winners), by
    path. force: the files (profile keys) to put in the package despite problems with their rescale.
    Fails (SmoothUIError) for a file patch doesn't hold."""
    force = set(force)
    results = {result.path: result for result in [*patch.patched, *patch.kept, *patch.failed]}
    if unknown := sorted(force - set(results)):
        fail(f"not a file the merged patches could hold: {', '.join(unknown)}")

    issues = _issues(patch)
    files = []
    for path in sorted(results):
        result = results[path]
        file = scan.files[path]
        source = file.winner
        assert source is not None   # patch holds the winners
        fallback = file.loaded_without_package.folder if file.loaded_without_package else None
        if isinstance(result, FailedFile):
            files.append(PackageFile(path, source, FileStatus.FAILED, error=result.error, fallback=fallback))
            continue
        status = FileStatus.PATCHED if isinstance(result, PatchedFile) else FileStatus.UNCHANGED
        file_issues = issues[path]
        if status is FileStatus.UNCHANGED:
            # Not rescaled, so its rescale settings (which then match nothing) and warnings don't apply
            file_issues = [issue for issue in file_issues if issue.kind is IssueKind.FIX]
        if status is FileStatus.PATCHED and any(issue.keeps_out for issue in file_issues):
            known_broken = any(issue.kind is IssueKind.KNOWN_BROKEN for issue in file_issues)
            status = (FileStatus.FORCED if path in force else
                      FileStatus.KNOWN_BROKEN if known_broken else FileStatus.LEFT_OUT)
        files.append(PackageFile(path, source, status, file_issues, result, fallback=fallback))
    return _whole_groups(scan, files)


def _whole_groups(scan: Scan, files: list[PackageFile]) -> list[PackageFile]:
    """files, with every file of a group that can't be merged whole left out (GROUP_LEFT_OUT), and each group
    file's group set"""
    groups = defaultdict(set)   # group: its files (profile keys)
    for key, group in scan.group_of.items():
        groups[group].add(key)
    by_path = {file.path: file for file in files}
    def merged(file):   # in the package, or needs no patch
        return file is not None and (file.status.included or file.status is FileStatus.UNCHANGED)
    causes = {}     # group: why it can't be merged whole
    for group, keys in sorted(groups.items()):
        missing = sorted(key for key in keys if not merged(by_path.get(key)))
        broken = [file_name(key) for key in missing if key in by_path and by_path[key].status is FileStatus.KNOWN_BROKEN]
        cant = [file_name(key) for key in missing if key in by_path and file_name(key) not in broken]
        no_copy = [file_name(key) for key in missing if key not in by_path]
        found = ([f"{_join(broken)} {'is' if len(broken) == 1 else 'are'} known not to work"] if broken else []) + \
                ([f"{_join(cant)} can't be merged"] if cant else []) + \
                ([f"{_join(no_copy)} {'has' if len(no_copy) == 1 else 'have'} no copy to merge"] if no_copy else [])
        if found:
            causes[group] = "; ".join(found)
    result = []
    for file in files:
        group = scan.group_of.get(file.path)
        if group in causes and merged(file):
            file = replace(file, status=FileStatus.GROUP_LEFT_OUT, error=causes[group])
        result.append(replace(file, group=group) if group else file)
    return result


def _issues(patch: ReleasePatch) -> dict[str, list[Issue]]:
    """{path: its Issues}"""
    issues = defaultdict(list)
    for issue in [*patch.rule_issues.errors, *patch.rule_issues.warnings]:
        # Fixes are judged by their results below (a fix that wasn't needed is fine). The merger's
        # profile has no "*" settings, whose issues have no path.
        if issue.fix is None and issue.path is not None:
            issues[issue.path].append(Issue(IssueKind.RULE, issue.message))
    for review, occurrences in patch.warnings.unreviewed.items():
        for occurrence in occurrences:
            known_broken = review in patch.known_broken.get(occurrence.path, ())
            issues[occurrence.path].append(Issue(IssueKind.KNOWN_BROKEN if known_broken else IssueKind.WARNING,
                                                 f"{occurrence.message} [{review}]", review=review))
    for result in [*patch.patched, *patch.kept]:
        for fix in result.report.fixes:
            if fix.outcome is FixOutcome.CANT_APPLY:
                issues[result.path].append(Issue(IssueKind.FIX, fix.detail, fix.name))
    return issues


def _join(names):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
