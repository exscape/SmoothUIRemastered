"""Which patched files go into the package, and what to tell the user about each file.

- A file that fails to patch (any exception) leaves out its mod: every file whose copy would come from it.
  A failure means the patcher can't handle that mod's files, so its others are suspect too. This can't
  be overridden.
- A fix that can't apply keeps nothing out: the file is used without the fix, and the user is told what
  that means (the fix's CONSEQUENCE).
- Other problems with a rescale (a setting that wasn't followed or no longer matches, an unreviewed
  warning) leave out that file only, unless the user forces it (force).

A file left out isn't in the package, so the game loads the copy it would load without the package: the
mod's own copy at its original frame rate, or the copy of a mod (Smooth UI's own, a compat patch) that
comes before it in the load order.

decide() is cheap and pure: a GUI can call it again with other choices without patching again.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from ..build.release import ReleasePatch
from ..common import fail
from ..patcher.fixes import CANT_APPLY, fix_consequence, fix_kind
from ..patcher.patcher_core import SourceFile
from . import REPORT_URL
from .installed import is_smoothui_folder
from .scan import COMPAT_PATCH, Scan


class FileStatus(Enum):
    PATCHED = "patched"             # in the package
    FORCED = "forced"               # in the package although its rescale had problems, as the user asked
    LEFT_OUT = "left out"           # its rescale had problems, so it isn't in the package (it can be forced)
    UNCHANGED = "unchanged"         # nothing changed it: already at the target frame rate, and each of its
                                    # fixes wasn't needed (or can't apply): not needed in the package
    FAILED = "failed"               # couldn't be patched, so its mod is left out
    MOD_FAILED = "mod failed"       # left out, as another file of its mod couldn't be patched

    @property
    def included(self) -> bool:
        return self in (FileStatus.PATCHED, FileStatus.FORCED)


class IssueKind(Enum):
    RULE = "rule"           # a setting that wasn't followed, or that matched in the release but matches nothing
                            # in this mod's copy (the mod changed what it was for)
    WARNING = "warning"     # a patcher warning that hasn't been reviewed
    FIX = "fix"             # a fix that can't apply to this mod's copy


@dataclass(frozen=True, slots=True)
class Issue:
    kind: IssueKind
    message: str
    fix: str | None = None      # for FIX: the fix's name

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
    error: str | None = None        # FAILED: why
    fallback: str | None = None     # the mod whose copy the game loads unless the package holds this file

    @property
    def mod(self) -> str:
        return self.source.origin

    @property
    def entry(self):
        """The patched file, as a BundleEntry (for a file the package holds)"""
        return self.result["entry"]

    @property
    def fixes_not_applied(self) -> list[str]:
        return [issue.fix for issue in self.issues if issue.kind == IssueKind.FIX]

    @property
    def severity(self) -> str:
        """"ok"; "warning" (not as Smooth UI would patch it); or "error" (not in the package, although it
        needs to be)"""
        if self.status in (FileStatus.PATCHED, FileStatus.UNCHANGED) and not self.issues:
            return "ok"
        if self.status.included or self.status == FileStatus.UNCHANGED:
            return "warning"
        return "error"

    def explain(self) -> str:
        """What happened to the file, for the user: if it isn't patched as Smooth UI would, why, and what
        they can do. The first line is the summary; the others are indented."""
        status, mod = self.status, self.mod
        if status == FileStatus.PATCHED:
            summary = f"patched ({mod}'s copy)" + (", but not entirely" if self.issues else "")
            details = self._fix_lines()
        elif status == FileStatus.UNCHANGED:
            if self.issues:
                summary = (f"{mod}'s copy is already at the target frame rate, and is used as it is, without these "
                           "fixes, which can't apply to it:")
            else:
                summary = f"{mod}'s copy is already at the target frame rate and needs none of its fixes: used as it is"
            details = self._fix_lines()
        elif status == FileStatus.FORCED:
            summary = (f"patched ({mod}'s copy) as you asked, although Smooth UI can't patch it entirely. It may "
                       "look or behave wrong in places:")
            details = [*self._issue_lines(), *self._fix_lines()]
        elif status == FileStatus.MOD_FAILED:
            summary = (f"NOT patched: another of {mod}'s files can't be patched (see there), so all of its files "
                       f"are left out. {self._fallback_text()}")
            details = []
        elif status == FileStatus.FAILED:
            summary = f"NOT patched: Smooth UI can't patch {mod}'s copy, so all of {mod}'s files are left out. {self._fallback_text()}"
            details = [f"Why: {self.error}",
                       (f"This is most likely a limitation or a bug of Smooth UI, not a mistake of {mod}: please report it at "
                        f"{REPORT_URL}, with this log.")]
        else:
            summary = f"NOT patched: Smooth UI can't patch {mod}'s copy entirely, so it is left out. {self._fallback_text()}"
            details = ["Why:", *self._issue_lines(), *self._fix_lines(),
                       ("What you can do: check for a newer version of the merger, or patch the file anyway (force "
                        "it) if you accept that it may look or behave wrong in places. This is most likely a "
                        f"limitation of Smooth UI, not a mistake of {mod}: please report it at {REPORT_URL}, "
                        "with this log.")]
        return "\n  ".join([f"{self.path}: {summary}", *details])

    def _issue_lines(self):
        return [f"- {issue.message}" for issue in self.issues if issue.keeps_out]

    def _fix_lines(self):
        return [f"- {fix_kind(issue.fix).header} {fix_consequence(issue.fix)} ({issue.fix} can't apply: {issue.message})"
                for issue in self.issues if issue.kind == IssueKind.FIX]

    def _fallback_text(self):
        return f"The game shows {self._fallback()}."

    def _fallback(self):
        """The copy the game shows unless the package holds the file"""
        if self.fallback is None or self.fallback == self.mod:
            return f"{self.mod}'s own copy, at its original frame rate"
        if is_smoothui_folder(self.fallback):
            return (f"Smooth UI's own copy, which comes before {self.mod}'s in the load order, without {self.mod}'s "
                    "changes to this screen")
        if COMPAT_PATCH.fullmatch(self.fallback):
            return f"the copy of {self.fallback} (a Smooth UI compat patch)"
        return f"{self.fallback}'s copy, which comes before {self.mod}'s in the load order"


def decide(scan: Scan, patch: ReleasePatch, *, force=()) -> list[PackageFile]:
    """A PackageFile for each file patch holds (patch_as_release's result for the scan's winners), by
    path. force: the files (profile keys) to put in the package despite problems with their rescale.
    Fails (SmoothUIError) for a file patch doesn't hold."""
    force = set(force)
    results = {result["path"]: (status, result) for status, results in
               ((FileStatus.PATCHED, patch.patched), (FileStatus.UNCHANGED, patch.kept), (FileStatus.FAILED, patch.failed))
               for result in results}
    if unknown := sorted(force - set(results)):
        fail(f"not a file the package could hold: {', '.join(unknown)}")

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
            status = FileStatus.FORCED if path in force else FileStatus.LEFT_OUT
        files.append(PackageFile(path, source, status, file_issues, result, fallback=fallback))
    return files


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
            issues[occurrence.path].append(Issue(IssueKind.WARNING, f"{occurrence.message} [{review}]"))
    for result in [*patch.patched, *patch.kept]:
        for fix in result["report"].get("fixes", []):
            if fix["result"] == CANT_APPLY:
                issues[result["path"]].append(Issue(IssueKind.FIX, fix["detail"], fix["name"]))
    return issues
