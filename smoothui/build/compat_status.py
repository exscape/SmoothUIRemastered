"""Tell which compat patches (their manifests in manifests/compat/, written by build-compat.py) a build of
Smooth UI Remastered makes outdated: build-main.py --make-release checks them all at the end, and
version-tool.py does it on request.

A patch is outdated when the release patches a file the patch ships differently from the build the
patch was made from (other settings, fixes or patcher code: the output differs), no longer patches one
of them, or patches a file the other mod ships that the patch lacks. Only those files count: version
names don't, and neither do changes to other files.

The other way around, build-compat.py tells when a new patch is not needed: when it ships the same files
with the same output as the other mod's newest earlier patch (see compare_with_earlier_patch), e.g. for
a new version of the other mod that didn't change any of the files it shares with Smooth UI Remastered.
"""

from __future__ import annotations

import logging

from ..common import fail, profile_key
from ..log import HEADING
from ..manifests import shipped_files
from .manifest_diff import describe_commit, file_changes
from .shared import (
    COMPAT_MANIFEST_DIR,
    REPO_ROOT,
    build_fps,
    load_manifest,
    version_key,
)

log = logging.getLogger(__name__)


def newest_compat_patches(version: str, mod_name: str | None = None, exclude=None) -> list[tuple]:
    """(path, manifest) of the newest compat patch of each other mod (or only of mod_name), among those
    built for this build's frame rate from this release or an earlier one (a patch built for a later
    release, e.g. a dev build, says nothing about this one). Newest: for the mod's latest version (mod
    versions version_key can't read count as the oldest), then built for the latest release, then the
    most recently built. Leaves out the manifest at exclude."""
    key, fps = version_key(version), build_fps(version)
    newest: dict[str, tuple] = {}  # mod name -> (rank, path, manifest)
    for path in COMPAT_MANIFEST_DIR.glob("*.json"):
        if path == exclude:
            continue
        patch = load_manifest(path)
        built_for = version_key(patch["smooth_version"])
        if built_for is None or key is None or built_for > key or build_fps(patch["smooth_version"]) != fps:
            continue
        mod = patch["mod_name"].lower()
        if mod_name is not None and mod != mod_name.lower():
            continue
        rank = (version_key(patch["mod_version"]) or (), built_for, patch["created_at"])
        if mod not in newest or rank > newest[mod][0]:
            newest[mod] = (rank, path, patch)
    return [(path, patch) for _, path, patch in sorted(newest.values(), key=lambda item: item[2]["mod_name"].lower())]


def compat_patch_problems(patch: dict, release: dict) -> list[str]:
    """Why a compat patch (its manifest) needs a rebuild for a release (its manifest), as lines of text;
    none if it works with that release as it is"""
    version = release["created_mod_version"]
    ours = {profile_key(path): record for path, record in shipped_files(release).items()}
    # The build the patch was made from, for the details; gone or rewritten if that release was rebuilt since
    built_from = load_manifest(patch["smooth_version"]) or {"files": {}}
    built_from_files = {profile_key(path): record for path, record in built_from["files"].items()}

    problems = []
    for path, record in sorted(patch["files"].items()):
        new = ours.get(profile_key(path))
        if new is None:
            problems.append(f"{path}: no longer patched by {version}, but the patch still ships it")
        elif new["output_swf_sha256"] != record["release_output_swf_sha256"]:
            problems.append(f"{path}: {version} patches it differently than {patch['smooth_version']}")
            old = built_from_files.get(profile_key(path))
            if old is not None and old["output_swf_sha256"] == record["release_output_swf_sha256"]:
                problems += [f"  {change}" for change in file_changes(old, new, compare_reports=True)]
                problems.append(f"  Patcher commit: {describe_commit(built_from)} -> {describe_commit(release)}")
            else:
                problems.append(f"  (no details: {patch['smooth_version']} was rebuilt since the patch was built)")
    shipped = {profile_key(path) for path in patch["files"]}
    problems += [f"{path}: now patched by {version}, but missing from the patch (the other mod ships it)"
                 for path in patch["other_mod_files"] if profile_key(path) in ours and profile_key(path) not in shipped]
    return problems


def print_compat_status(version_or_path, patches: list[tuple] | None = None, mod_name: str | None = None) -> int:
    """Print whether a build of a release (its version, e.g. v0.3-60fps, or its manifest's path) makes
    compat patches outdated: patches ([(path, manifest)]), or by default the newest of each other mod's
    (see newest_compat_patches; only mod_name's, if given). Returns how many are outdated."""
    release = load_manifest(version_or_path)
    if release is None:
        fail(f"manifest not found: {version_or_path}")
    version = release["created_mod_version"]
    log.info(f"Compat patches for {version}", extra=HEADING)
    if patches is None:
        patches = newest_compat_patches(version, mod_name)
    if not patches:
        log.info(f"No {build_fps(version)} fps compat patch manifests{f' of {mod_name}' if mod_name else ''} built "
                 f"for {version} or earlier in {COMPAT_MANIFEST_DIR}")
        return 0

    results = [(path, patch, compat_patch_problems(patch, release)) for path, patch in patches]
    for _, patch, problems in results:
        log.info(f"{'OUTDATED' if problems else 'Up to date':<10}  {_describe(patch)}")
    outdated = [result for result in results if result[2]]
    for path, patch, problems in outdated:
        shown_path = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        log.info(f"\n{_describe(patch)}  [{shown_path}]")
        log.info("\n".join(f"  {problem}" for problem in problems))
    if outdated:
        log.warning(f"\n{len(outdated)} compat patch(es) need a rebuild for {version}. For a patch that was "
                    "never released, delete its manifest instead.")
    log.info("")
    return len(outdated)


def _describe(patch: dict) -> str:
    return f"{patch['mod_name']} {patch['mod_version']} (built for {patch['smooth_version']})"
