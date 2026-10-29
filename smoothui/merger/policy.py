"""Which patched files go into the package, and what to tell the user about each file.

- A file that fails to patch (any exception) leaves out its mod: every file whose copy would come from it.
  A failure means the patcher can't handle that mod's files, so its others are suspect too. This can't
  be overridden.
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

A file left out isn't in the package, so the game loads the copy it would load without the package: the
mod's own copy at its original frame rate, or the copy of a mod (Smooth UI's own, a compat patch) that
comes before it in the load order.

decide() is cheap and pure: a GUI can call it again with other choices without patching again.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import Enum

from ..build.release import ReleasePatch
from ..common import fail
from ..patcher.fixes import CANT_APPLY, fix_consequence, fix_kind
from ..witcher3.source_file import GAME_FILES, SourceFile
from .installed import is_smoothui_folder
from .scan import COMPAT_PATCH, Scan
from .screens import file_name, screen_label
from .state import WHY, not_merged, original_frame_rate


class FileStatus(Enum):
    PATCHED = "patched"             # in the package
    FORCED = "forced"               # in the package although its rescale had problems, as the user asked
    LEFT_OUT = "left out"           # its rescale had problems, so it isn't in the package (it can be forced)
    KNOWN_BROKEN = "known broken"   # its mod's copy is known not to work with Smooth UI, so it isn't in the
                                    # package (it can be forced)
    UNCHANGED = "unchanged"         # nothing changed it: already at the target frame rate, and each of its
                                    # fixes wasn't needed (or can't apply): not needed in the package
    FAILED = "failed"               # couldn't be patched, so its mod is left out
    MOD_FAILED = "mod failed"       # left out, as another file of its mod couldn't be patched
    GROUP_LEFT_OUT = "group left out"   # left out, as its group can't be merged whole (see the module's doc)

    @property
    def included(self) -> bool:
        return self in (FileStatus.PATCHED, FileStatus.FORCED)


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
        return self.kind != IssueKind.FIX


@dataclass(frozen=True, slots=True)
class PackageFile:
    """One file the package could hold: a file Smooth UI patches, whose winning copy is another mod's"""
    path: str                       # profile key
    source: SourceFile              # the copy that was patched
    status: FileStatus
    issues: list[Issue] = field(default_factory=list)
    result: dict | None = None      # patch_files' result for it, unless it failed
    error: str | None = None        # FAILED: why; GROUP_LEFT_OUT: why the group can't be merged whole
    fallback: str | None = None     # the mod whose copy the game loads unless the package holds this file
    group: str | None = None        # the group it is in, if any (see build/profile.py's Group)

    @property
    def mod(self) -> str:
        return self.source.origin

    @property
    def forced(self) -> bool:
        return self.status == FileStatus.FORCED

    @property
    def entry(self):
        """The patched file, as a BundleEntry (for a file the package holds)"""
        return self.result["entry"]

    @property
    def known_broken(self) -> list[str]:
        """The reviews of the warnings that mean its copy is known not to work"""
        return [issue.review for issue in self.issues if issue.kind == IssueKind.KNOWN_BROKEN]

    @property
    def fixes_not_applied(self) -> list[str]:
        return [issue.fix for issue in self.issues if issue.kind == IssueKind.FIX]

    @property
    def severity(self) -> str:
        """"ok" (as Smooth UI would patch it, or patched anyway as the user chose, or known not to work);
        "warning" (without some fixes); or "error" (not in the package, although it needs to be)"""
        if self.status == FileStatus.KNOWN_BROKEN:
            return "ok"
        if self.status in (FileStatus.PATCHED, FileStatus.UNCHANGED, FileStatus.FORCED) and not self.fixes_not_applied:
            return "ok"
        # A group left out whole is consistent (the file that keeps it out is the error)
        if self.status.included or self.status in (FileStatus.UNCHANGED, FileStatus.GROUP_LEFT_OUT):
            return "warning"
        return "error"

    @property
    def reason(self) -> str | None:
        """Why its rescale has problems, in a word for the user (see state.WHY): "code" (a warning that isn't
        reviewed) or "changes" (a setting that wasn't followed, or no longer matches); None if neither"""
        kinds = {issue.kind for issue in self.issues}
        return "code" if IssueKind.WARNING in kinds else "changes" if IssueKind.RULE in kinds else None

    @property
    def to_report(self) -> bool:
        """Whether it is worth a bug report: left out for a problem (not known not to work), or failed"""
        return self.status in (FileStatus.LEFT_OUT, FileStatus.FAILED, FileStatus.MOD_FAILED)

    def explain(self, label: str | None = None) -> str:
        """What happened to the file, for the user, in short: if it isn't patched as Smooth UI would, why (in
        plain words), and what the game shows instead. The first line is the summary, after label (default: its
        screen_label); the others are indented: the fixes it lacks, and the technical details. A group's files
        left out with it say the same, so that they can be told as one (see screens.by_group)."""
        status, copy = self.status, self._copy()
        if status == FileStatus.PATCHED:
            summary = f"merged ({copy})" + (", without some fixes:" if self.issues else "")
        elif status == FileStatus.UNCHANGED:
            summary = f"{copy[0].upper() + copy[1:]} is already smooth: used as it is" + (", without some fixes:" if self.issues else "")
        elif status == FileStatus.FORCED:
            summary = (f"merged anyway, as you chose: {copy} {WHY.get(self.reason) or WHY['left out']}, so it may look "
                       "or behave wrong in places" + (". Without some fixes:" if self.fixes_not_applied else "."))
        elif status == FileStatus.GROUP_LEFT_OUT:
            summary = f"{not_merged(status.value, copy, self.mod, group=self.group, kept_out_by=self.error)} {self._runs()}"
        else:
            reason = self.reason if status == FileStatus.LEFT_OUT else None
            summary = f"{not_merged(status.value, copy, self.mod, reason=reason)} {self._runs()}"
        lines = self._fix_lines() if status.included or status == FileStatus.UNCHANGED else []
        if details := self._details():
            lines.append(f"Details: {details}")
        return "\n  ".join([f"{screen_label(self.path) if label is None else label}: {summary}", *lines])

    def _details(self):
        """The technical reasons, for the log: the warnings' reviews (see mod_reviews.py), the settings'
        problems, the failure, the fixes that can't apply; none for a file left out with its group or its mod"""
        if self.status == FileStatus.FAILED:
            return self.error
        if self.status in (FileStatus.GROUP_LEFT_OUT, FileStatus.MOD_FAILED):
            return None
        shown = self.issues
        if self.status == FileStatus.KNOWN_BROKEN:
            shown = [issue for issue in shown if issue.kind == IssueKind.KNOWN_BROKEN]
        elif self.status in (FileStatus.PATCHED, FileStatus.UNCHANGED):
            shown = [issue for issue in shown if issue.kind == IssueKind.FIX]
        return "; ".join(issue.review if issue.review else f"{issue.fix}: {issue.message}" if issue.fix else issue.message
                         for issue in shown) or None

    def _fix_lines(self):
        return [f"- {fix_kind(issue.fix).header} {fix_consequence(issue.fix)} ({issue.fix})"
                for issue in self.issues if issue.kind == IssueKind.FIX]

    def _copy(self):
        """The copy that was patched, for the user: the game's copy, or <mod>'s copy"""
        return "the game's copy" if self.mod == GAME_FILES else f"{self.mod}'s copy"

    def _runs(self):
        """What the game shows instead, as a sentence: the file's own copy (said of its whole group, if it is
        left out with it), or another"""
        if self.fallback is None or self.fallback == self.mod:
            return original_frame_rate(self.group if self.status == FileStatus.GROUP_LEFT_OUT else None)
        return f"The game uses {self._fallback()} instead."

    def _fallback(self):
        """The copy the game shows unless the package holds the file, when it isn't the file's own"""
        if is_smoothui_folder(self.fallback):
            return f"Smooth UI's own copy, without {self.mod}'s changes (it comes first in the load order)"
        if COMPAT_PATCH.fullmatch(self.fallback):
            return f"the copy of {self.fallback} (a Smooth UI compat patch)"
        return f"{self.fallback}'s copy (it comes before {self.mod}'s in the load order)"


def decide(scan: Scan, patch: ReleasePatch, *, force=()) -> list[PackageFile]:
    """A PackageFile for each file patch holds (patch_as_release's result for the scan's winners), by
    path. force: the files (profile keys) to put in the package despite problems with their rescale.
    Fails (SmoothUIError) for a file patch doesn't hold."""
    force = set(force)
    results = {result["path"]: (status, result) for status, results in
               ((FileStatus.PATCHED, patch.patched), (FileStatus.UNCHANGED, patch.kept), (FileStatus.FAILED, patch.failed))
               for result in results}
    if unknown := sorted(force - set(results)):
        fail(f"not a file the merged patches could hold: {', '.join(unknown)}")

    issues = _issues(patch)
    failed_mods = {result["origin"] for result in patch.failed}
    files = []
    for path in sorted(results):
        status, result = results[path]
        file = scan.files[path]
        source = file.winner
        fallback = file.loaded_without_package.folder if file.loaded_without_package else None
        if status == FileStatus.FAILED:
            files.append(PackageFile(path, source, status, error=result["error"], fallback=fallback))
            continue
        file_issues = issues[path]
        if status == FileStatus.UNCHANGED:
            # Not rescaled, so its rescale settings (which then match nothing) and warnings don't apply
            file_issues = [issue for issue in file_issues if issue.kind == IssueKind.FIX]
        if source.origin in failed_mods:
            status = FileStatus.MOD_FAILED
        elif status == FileStatus.PATCHED and any(issue.keeps_out for issue in file_issues):
            known_broken = any(issue.kind == IssueKind.KNOWN_BROKEN for issue in file_issues)
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
        return file is not None and (file.status.included or file.status == FileStatus.UNCHANGED)
    causes = {}     # group: why it can't be merged whole
    for group, keys in sorted(groups.items()):
        missing = sorted(key for key in keys if not merged(by_path.get(key)))
        cant = [file_name(key) for key in missing if key in by_path]
        no_copy = [file_name(key) for key in missing if key not in by_path]
        found = ([f"{_join(cant)} can't be merged"] if cant else []) + \
                ([f"{_join(no_copy)} {'has' if len(no_copy) == 1 else 'have'} no copy to merge"] if no_copy else [])
        if found:
            causes[group] = " and ".join(found)
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
        for fix in result["report"].get("fixes", []):
            if fix["result"] == CANT_APPLY:
                issues[result["path"]].append(Issue(IssueKind.FIX, fix["detail"], fix["name"]))
    return issues


def _join(names):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
