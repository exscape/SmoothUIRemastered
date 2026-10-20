"""Tell whether the game's files (the uncooked files a build patches) changed since a build: a game patch
may change some of the files Smooth UI patches, which then need a new release, as do the compat
patches that ship them. build-main.py checks this before building; version-tool.py --check-game-files
on request.

A file changed if it is missing, has another size, or (same size) another SHA-256 than the manifest
recorded for its source.
"""

from __future__ import annotations

from ..common import profile_key
from .compat_status import newest_compat_patches
from .shared import load_manifest, sha256_file


def changed_game_files(uncooked_dir, manifest: dict) -> dict[str, str]:
    """{path: how it changed} for the files a build (its manifest) patched whose source in uncooked_dir
    differs from the one the build was made from"""
    changed = {}
    for path, record in manifest["files"].items():
        src = uncooked_dir / path
        if not src.is_file():
            changed[path] = "missing from the game's files"
        # Manifests from before the 60 and 120 fps builds (no "fps") recorded the output's size as the source's
        elif "fps" in manifest and src.stat().st_size != record["source_size"]:
            changed[path] = f"size {record['source_size']} -> {src.stat().st_size} bytes"
        elif sha256_file(src) != record["source_sha256"]:
            changed[path] = "different contents (same size)"
    return changed


def game_file_report(uncooked_dir, builds) -> tuple[str, int]:
    """(text, how many changed) telling which files that builds (manifest paths) patched have changed in
    uncooked_dir since, and which compat patches ship them. Log the text as a warning if any changed."""
    manifests = [load_manifest(path) for path in builds]
    versions = ", ".join(manifest["created_mod_version"] for manifest in manifests)
    checked = {path for manifest in manifests for path in manifest["files"]}
    changed: dict[str, str] = {}
    for manifest in manifests:
        changed.update(changed_game_files(uncooked_dir, manifest))
    if not changed:
        return f"All {len(checked)} files patched by {versions} are unchanged in {uncooked_dir}", 0

    lines = [(f"{len(changed)} of the {len(checked)} files patched by {versions} changed in {uncooked_dir}: "
              "the mod needs a new release for them")]
    lines += [f"  CHANGED  {path}: {how}" for path, how in sorted(changed.items())]
    keys = {profile_key(path) for path in changed}
    affected = sorted({f"{patch['mod_name']} {patch['mod_version']}" for manifest in manifests
                       for _, patch in newest_compat_patches(manifest["created_mod_version"])
                       if keys & {profile_key(path) for path in patch["files"]}})
    if affected:
        lines.append("Compat patches that ship changed files, and so need a rebuild after it: " + ", ".join(affected))
    return "\n".join(lines), len(changed)
