"""The package: the merger's own mod folder in Mods\\ (scan.PACKAGE_FOLDER), holding the patched files, and the
mods' copies it holds as they are (see builder.as_is_copies).

Its files, all written by files.py's rules:
- content\\blob0.bundle and content\\metadata.store: the files the package holds (none, if it holds none)
- PACKAGE_MANIFEST: what it holds and what it was built from. Written last, and deleted first, so a
  package without one is incomplete (an interrupted build).

Nothing else in the folder is touched. A package that would hold no files isn't written: a mod folder
without bundles may look broken to the game or a mod manager, so the merger's files are deleted, and the
folder too, unless it holds other files. Its manifest then goes to a record in the merger's data folder
(see record_path) instead, so that what the build decided is still known.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common import SmoothUIError
from ..files import delete_file, make_dirs, read_file, remove_empty_dir, write_file
from ..manifests import patcher_commit
from ..witcher3.mod_bundles import pack_entries
from . import tools_version
from .installed import Installed
from .status import FileStatus, Reason

if TYPE_CHECKING:
    # Not imported otherwise: the launch check reads PACKAGE_MANIFEST, and policy.py imports the patcher
    from .policy import PackageFile

log = logging.getLogger(__name__)

PACKAGE_MANIFEST = "smoothui_package.json"
BUNDLE = "blob0.bundle"
STORE = "metadata.store"
# Every file the merger writes in the package, relative to it
OWN_FILES = (f"content/{BUNDLE}", f"content/{STORE}", PACKAGE_MANIFEST)
# The package manifest's format: increased when it changes in a way that older readers can't handle
PACKAGE_MANIFEST_FORMAT = 1
# The folder, in the merger's data folder, of the records of packages that hold no files
RECORDS = "packages"


@dataclass(frozen=True, slots=True)
class CopyRecord:
    """The copy a manifest record is about: what a build decided on, to tell whether it changed without
    reading it"""
    mod: str | None = None              # its mod's folder, or GAME_FILES
    bundle: str | None = None           # the bundle's file name
    source_size: int | None = None
    source_crc32: int | None = None

    def same_copy(self, source) -> bool:
        """Whether this is about a copy (a SourceFile): the same mod, bundle, size and CRC32"""
        return ((self.mod or "").lower() == source.origin.lower()
                and (self.bundle or "").lower() == source.file.name.lower()
                and self.source_size == source.size and self.source_crc32 == source.toc_entry.crc)

    def _copy_json(self) -> dict:
        return {"mod": self.mod, "bundle": self.bundle, "source_size": self.source_size,
                "source_crc32": self.source_crc32}


@dataclass(frozen=True, slots=True)
class HeldRecord(CopyRecord):
    """A file the package holds, patched"""
    output_size: int | None = None      # the patched copy, to tell whether the package is intact
    output_crc32: int | None = None
    forced: bool = False                # patched anyway, as the user chose: then also forced_since, reasons
                                        # and reason
    fixes_not_applied: tuple[str, ...] = ()
    speed: float | None = None          # see policy.PackageFile.speed
    forced_since: str | None = None     # the date it was first forced
    reasons: tuple[str, ...] = ()       # why it isn't fully patched (technical)
    reason: Reason | None = None        # why, in a word, for the user (see state.WHY)

    @classmethod
    def from_json(cls, data) -> HeldRecord:
        data = data if isinstance(data, dict) else {}
        return cls(**_copy_fields(data), output_size=_int(data.get("output_size")),
                   output_crc32=_int(data.get("output_crc32")), forced=bool(data.get("forced")),
                   fixes_not_applied=_texts(data.get("fixes_not_applied")), speed=_number(data.get("speed")),
                   forced_since=_text(data.get("forced_since")), reasons=_texts(data.get("reasons")),
                   reason=Reason.read(_text(data.get("reason"))))

    def to_json(self) -> dict:
        data = {**self._copy_json(), "output_size": self.output_size, "output_crc32": self.output_crc32,
                "forced": self.forced, "fixes_not_applied": list(self.fixes_not_applied)}
        if self.speed is not None:
            data["speed"] = self.speed
        if self.forced:
            data.update(forced_since=self.forced_since, reasons=list(self.reasons), reason=self.reason)
        return data


@dataclass(frozen=True, slots=True)
class NotHeldRecord(CopyRecord):
    """A file the package doesn't hold patched: status says why. A mod's copy may be held as it is (as_is)."""
    status: FileStatus = FileStatus.LEFT_OUT
    reasons: tuple[str, ...] = ()       # why (technical); a screen turned off has none
    reason: Reason | None = None        # LEFT_OUT: why, in a word (see HeldRecord)
    fps: float | None = None            # UNCHANGED: the frame rate its copy runs at
    speed: float | None = None          # UNCHANGED: see policy.PackageFile.speed
    known_broken: tuple[str, ...] = ()  # KNOWN_BROKEN: the reviews that say so (see policy.PackageFile.known_broken)
    as_is: bool = False                 # the package holds the mod's copy unpatched: then also these two
    output_size: int | None = None
    output_crc32: int | None = None
    cant_hold: bool = False             # it should, but the copy can't be read

    @classmethod
    def from_json(cls, data) -> NotHeldRecord:
        data = data if isinstance(data, dict) else {}
        return cls(**_copy_fields(data), status=FileStatus.read(_text(data.get("status"))),
                   reasons=_texts(data.get("reasons")), reason=Reason.read(_text(data.get("reason"))),
                   fps=_number(data.get("fps")), speed=_number(data.get("speed")),
                   known_broken=_texts(data.get("known_broken")), as_is=bool(data.get("as_is")),
                   output_size=_int(data.get("output_size")), output_crc32=_int(data.get("output_crc32")),
                   cant_hold=bool(data.get("cant_hold")))

    def to_json(self) -> dict:
        data = {**self._copy_json(), "status": self.status}
        if self.status not in (FileStatus.BY_CHOICE, FileStatus.BY_RULE):
            data["reasons"] = list(self.reasons)
        if self.status is FileStatus.LEFT_OUT:
            data["reason"] = self.reason
        if self.status is FileStatus.UNCHANGED:
            data["fps"] = self.fps
        if self.speed is not None:
            data["speed"] = self.speed
        if self.status is FileStatus.KNOWN_BROKEN:
            data["known_broken"] = list(self.known_broken)
        if self.cant_hold:
            data["cant_hold"] = True
        if self.as_is:
            data.update(as_is=True, output_size=self.output_size, output_crc32=self.output_crc32)
        return data


@dataclass(frozen=True, slots=True)
class BuiltFor:
    """The Smooth UI build a package was built for"""
    folder: str | None = None
    version: str | None = None
    fps: int | None = None              # the release's frame rate
    target_fps: int | None = None       # the frame rates it was built for (see scan.Scan.target_fps)
    hud_fps: int | None = None
    release_created_at: str | None = None   # a release's manifest is rewritten when it is rebuilt: which build


@dataclass(frozen=True, slots=True)
class PackageManifest:
    """The package manifest, PACKAGE_MANIFEST: what the package holds, and what it was built from (see
    package_manifest). read() fills in what older packages lack, and to_json() gives the file's layout."""
    tools_version: str | None = None
    patcher_commit: str | None = None
    patcher_uncommitted_changes: bool | None = None
    created_at: str | None = None
    smoothui: BuiltFor = BuiltFor()
    mod_reviews_sha256: str | None = None   # the reviews of mods' code used (see mod_reviews.py)
    optional_fixes: tuple[str, ...] = ()    # the optional fixes chosen (see scan.Scan.optional_fixes)
    files: dict[str, HeldRecord] = field(default_factory=dict)  # by profile key
    not_held: dict[str, NotHeldRecord] = field(default_factory=dict)
    groups_off: tuple[str, ...] = ()        # the groups turned off: their files are in neither of the above
    screens_off: tuple[str, ...] = ()       # the files the user chose not to merge: in neither either, unless
                                            # held as they are
    screens_off_by_rule: dict[str, str] = field(default_factory=dict)  # which of those our rules left out (a
                                            # dont-merge line, see mod_reviews.py), and why: not the user's choice

    @classmethod
    def read(cls, path) -> tuple[PackageManifest | None, str | None]:
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
        return cls.from_json(manifest), None

    @classmethod
    def from_json(cls, data: dict) -> PackageManifest:
        """From the file's JSON. Anything missing or of the wrong type counts as not recorded."""
        built = _dict(data.get("smoothui"))
        return cls(
            tools_version=_text(data.get("tools_version")), patcher_commit=_text(data.get("patcher_commit")),
            patcher_uncommitted_changes=_bool(data.get("patcher_uncommitted_changes")),
            created_at=_text(data.get("created_at")),
            smoothui=BuiltFor(
                folder=_text(built.get("folder")), version=_text(built.get("version")), fps=_int(built.get("fps")),
                # Older packages: built for the release's frame rate
                target_fps=_int(built.get("target_fps", built.get("fps"))),
                hud_fps=_int(built.get("hud_fps", built.get("fps"))),
                release_created_at=_text(built.get("release_created_at"))),
            mod_reviews_sha256=_text(data.get("mod_reviews_sha256")), optional_fixes=_texts(data.get("optional_fixes")),
            files={key: HeldRecord.from_json(record) for key, record in _dict(data.get("files")).items()},
            not_held={key: NotHeldRecord.from_json(record) for key, record in _dict(data.get("not_held")).items()},
            groups_off=_texts(data.get("groups_off")), screens_off=_texts(data.get("screens_off")),
            screens_off_by_rule={key: reason for key, reason in _dict(data.get("screens_off_by_rule")).items()
                                 if isinstance(reason, str)})

    def to_json(self) -> dict:
        return {
            "format": PACKAGE_MANIFEST_FORMAT,
            "tools_version": self.tools_version,
            "patcher_commit": self.patcher_commit,
            "patcher_uncommitted_changes": self.patcher_uncommitted_changes,
            "created_at": self.created_at,
            "smoothui": asdict(self.smoothui),
            "mod_reviews_sha256": self.mod_reviews_sha256,
            "optional_fixes": list(self.optional_fixes),
            "files": {key: record.to_json() for key, record in self.files.items()},
            "not_held": {key: record.to_json() for key, record in self.not_held.items()},
            "groups_off": list(self.groups_off),
            "screens_off": list(self.screens_off),
            "screens_off_by_rule": self.screens_off_by_rule,
        }

    def to_bytes(self) -> bytes:
        """The file's content"""
        return (json.dumps(self.to_json(), indent=1, sort_keys=True) + "\n").encode("utf-8")


def _copy_fields(data: dict) -> dict[str, Any]:
    """CopyRecord's fields, from a record's JSON"""
    return {"mod": _text(data.get("mod")), "bundle": _text(data.get("bundle")),
            "source_size": _int(data.get("source_size")), "source_crc32": _int(data.get("source_crc32"))}


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def _int(value) -> int | None:
    return value if isinstance(value, int) else None


def _number(value) -> float | None:
    return value if isinstance(value, (int, float)) else None


def _bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _texts(value) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class Written:
    """What write_package returns"""
    manifest: PackageManifest   # in the package, or in its record if it holds no files
    unknown_files: list[str]    # files in the package folder that the merger didn't write, relative to it
    removed: bool = False       # it holds no files, and its folder is gone (or never was there)

    @property
    def as_is(self) -> int:
        """How many mods' copies it holds as they are"""
        return sum(record.as_is for record in self.manifest.not_held.values())

    @property
    def empty(self) -> bool:
        """Whether it holds no files, so there is no package"""
        return not self.manifest.files and not self.as_is


def record_path(data_dir: Path, mods_dir: Path) -> Path:
    """Where the record of the package for a Mods\\ folder goes, while it holds no files: one file per Mods\\
    folder, in data_dir's RECORDS"""
    key = os.path.normcase(os.path.abspath(mods_dir)).encode("utf-8")
    return Path(data_dir) / RECORDS / f"{hashlib.sha256(key).hexdigest()[:16]}.json"


def write_package(package_dir: Path, installed: Installed, files: list[PackageFile], record: Path | None = None, *,
                  reviews_sha256: str | None, groups_off=(), screens_off=(), rates=(None, None), as_is=(),
                  optional_fixes=()) -> Written:
    """Write the package into package_dir (created if needed), replacing what it held: the files in files
    that are to be included (see policy.FileStatus.included), and a manifest recording files (see
    package_manifest). installed: the Smooth UI build they were patched for; reviews_sha256: the reviews of
    mods' code they were patched with (see mod_reviews.py); groups_off and screens_off: the groups and
    files turned off (see scan.Scan.groups_off and screens_off; the latter is a dict); rates: the menus' and the
    HUD's frame rates they were patched for (see scan.Scan.target_fps; None: installed's); as_is: the mods'
    copies to hold as they are (see policy.AsIs); optional_fixes: see scan.Scan.optional_fixes. If it would hold
    no files, remove the package instead, and write the manifest to record (see above), if given; else delete
    record."""
    package_dir = Path(package_dir)
    content = package_dir / "content"
    previous, _ = PackageManifest.read(package_dir / PACKAGE_MANIFEST)
    delete_file(package_dir / PACKAGE_MANIFEST)
    included = sorted((file for file in files if file.status.included), key=lambda file: file.path)
    manifest = package_manifest(installed, files, previous, reviews_sha256, groups_off, screens_off, rates, as_is,
                                optional_fixes)
    entries = [*(file.entry for file in included), *(copy.entry for copy in as_is if copy.entry is not None)]
    if not entries:
        removed, unknown = remove_package(package_dir)
        log.info("Nothing to merge: " + ("removed the merged patches" if removed else "their folder stays, as it holds other files"))
        if record is not None:
            make_dirs(Path(record).parent)
            write_file(record, manifest.to_bytes())
        return Written(manifest, unknown, removed)

    bundle, store = pack_entries(entries, BUNDLE)
    make_dirs(content)
    write_file(content / BUNDLE, bundle)
    write_file(content / STORE, store)
    log.info(f"Wrote {content / BUNDLE} ({len(entries)} files, {len(bundle) / 2**20:.1f} MiB) and its {STORE}")
    write_file(package_dir / PACKAGE_MANIFEST, manifest.to_bytes())
    if record is not None:
        delete_file(record)
    return Written(manifest, _unknown_files(package_dir))


def remove_package(package_dir: Path) -> tuple[bool, list[str]]:
    """Delete the merger's own files in package_dir (OWN_FILES, the manifest first), then its folders if
    they are empty; nothing else. mods.settings is left alone: its entry for the package stays, as a later
    build would write it again. Returns whether the folder is gone (also if it never was there), and the
    files left in it that the merger didn't write."""
    package_dir = Path(package_dir)
    delete_file(package_dir / PACKAGE_MANIFEST)
    for name in (BUNDLE, STORE):
        delete_file(package_dir / "content" / name)
    remove_empty_dir(package_dir / "content")
    unknown = _unknown_files(package_dir)
    return not unknown and remove_empty_dir(package_dir), unknown


def package_manifest(installed: Installed, files: list[PackageFile], previous: PackageManifest | None = None,
                     reviews_sha256: str | None = None, groups_off=(), screens_off=(), rates=(None, None), as_is=(),
                     optional_fixes=()) -> PackageManifest:
    """What the package holds, and what it was built from: for each file it holds, the copy it was patched
    from (to tell whether that changed without reading it) and the output (to tell whether the package
    is intact); for each other file, why it isn't there, and the copy that was decided on (and for one left
    unchanged, its frame rate). A file that keeps a frame rate above the target also gets the speed it plays at
    (see policy.PackageFile.speed). A forced file also gets why it isn't fully patched, and the date it was first forced: from previous (the manifest
    of the package this one replaces) while its copy is the same, else today."""
    commit, uncommitted = patcher_commit()
    before = previous.files if previous else {}
    today = datetime.date.today().isoformat()
    held: dict[str, HeldRecord] = {}
    not_held: dict[str, NotHeldRecord] = {}
    assert installed.manifest is not None   # usable
    for file in files:
        assert file.source.toc_entry is not None    # a copy in a bundle
        source: dict[str, Any] = {"mod": file.mod, "bundle": file.source.file.name, "source_size": file.source.size,
                                  "source_crc32": file.source.toc_entry.crc}    # (CopyRecord's fields)
        if file.status.included:
            held[file.path] = HeldRecord(**source, output_size=file.entry.size, output_crc32=file.entry.crc,
                                         forced=file.forced, fixes_not_applied=tuple(file.fixes_not_applied),
                                         speed=file.speed)
            if file.forced:
                old = before.get(file.path)
                kept = old.forced_since if old and old.forced and old.same_copy(file.source) else None
                held[file.path] = replace(held[file.path], forced_since=kept or today,
                                          reasons=tuple(issue.message for issue in file.issues if issue.keeps_out),
                                          reason=file.reason)
        elif file.status is FileStatus.KNOWN_BROKEN:
            # Its other issues don't matter: it is left out anyway
            not_held[file.path] = NotHeldRecord(
                **source, status=file.status,
                reasons=tuple(issue.message for issue in file.issues if issue.review in file.known_broken),
                known_broken=tuple(file.known_broken))
        else:
            unchanged = file.status is FileStatus.UNCHANGED
            not_held[file.path] = NotHeldRecord(
                **source, status=file.status,
                reasons=(file.error,) if file.error else tuple(issue.message for issue in file.issues),
                reason=file.reason if file.status is FileStatus.LEFT_OUT else None,
                fps=file.fps if unchanged else None, speed=file.speed if unchanged else None)
    for copy in as_is:     # (It keeps its status: only where the game gets it from differs)
        record = not_held.get(copy.path) or NotHeldRecord(mod=copy.source.origin, status=copy.status)
        record = replace(record, bundle=copy.source.file.name, source_size=copy.source.size,
                         source_crc32=copy.source.toc_entry.crc)
        if copy.entry is None:
            not_held[copy.path] = replace(record, cant_hold=True)
        else:
            not_held[copy.path] = replace(record, as_is=True, output_size=copy.entry.size, output_crc32=copy.entry.crc)
    return PackageManifest(
        tools_version=tools_version(), patcher_commit=commit, patcher_uncommitted_changes=uncommitted,
        created_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        smoothui=BuiltFor(folder=installed.folder, version=installed.version, fps=installed.fps,
                          target_fps=rates[0] or installed.fps, hud_fps=rates[1] or installed.fps,
                          release_created_at=installed.manifest.get("created_at")),
        mod_reviews_sha256=reviews_sha256, optional_fixes=tuple(sorted(optional_fixes)), files=held, not_held=not_held,
        groups_off=tuple(sorted(groups_off)), screens_off=tuple(sorted(screens_off)),
        screens_off_by_rule={key: choice.reason for key, choice in sorted(dict(screens_off).items()) if choice.reason})


def _unknown_files(package_dir):
    """The files in package_dir that the merger didn't write (relative, with / separators)"""
    found = []
    for folder, _, names in os.walk(package_dir):
        for name in names:
            relative = Path(folder, name).relative_to(package_dir).as_posix()
            if relative.lower() not in {own.lower() for own in OWN_FILES}:
                found.append(relative)
    return sorted(found)
