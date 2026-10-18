"""manifest-tool.py: answers questions about Smooth UI Remastered builds from their manifests (manifests/,
and manifests/compat/ for the compat patches), without building anything:

--check-all-compat-patches  Which compat patches need a rebuild for the latest Smooth UI builds?
--check-compat-patch PATCH  Does one other mod's compat patch need a rebuild for them?
--diff OLD NEW              What changed from one release to another, file by file?

The checks use the latest manifest at each frame rate (dev builds included), or the release --against
names; see compat_status.py. The diff is manifest_diff.py's.
Exit status: 0, or 1 if a check found an outdated patch, or 2 for an invalid command line.
"""

import argparse
import sys
from pathlib import Path

from ..common import normalize_version
from .compat_status import newest_compat_patches, print_compat_status
from .manifest_diff import print_manifest_diff
from .shared import (
    COMPAT_MANIFEST_DIR,
    OLD_MANIFEST_FPS,
    build_version,
    load_manifest,
    manifest_path,
    release_manifests,
)

# The frame rates of a release's builds, in the order they are reported
FPS_LIST = (120, 60)

EXAMPLES = """examples:
  manifest-tool.py --check-all-compat-patches
      which compat patches the latest builds (e.g. v0.3.2dev-120fps and -60fps) make outdated
  manifest-tool.py --check-all-compat-patches --against v0.3.1
  manifest-tool.py --check-compat-patch SortEverything
  manifest-tool.py --check-compat-patch manifests/compat/v0.3.1-60fps_SortEverything_v1.55.json
  manifest-tool.py --diff v0.3.1 v0.3.2dev
      what changed from v0.3.1 to v0.3.2dev, for each frame rate (or e.g. v0.3.1-60fps for one build)"""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Check Smooth UI Remastered compat patches against releases, or compare two releases, "
                    "using their manifests (manifests/)",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-all-compat-patches", action="store_true",
                      help="tell which compat patches (manifests/compat/; only the latest version of each other "
                           "mod) need a rebuild for the latest Smooth UI builds, dev builds included")
    mode.add_argument("--check-compat-patch", metavar="PATCH",
                      help="the same for one other mod: a compat patch manifest's path, or the mod's name "
                           "(for its latest version's patches)")
    mode.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"),
                      help="show what changed from one release to another: versions (v0.3.1 for both of its "
                           "builds, v0.3.1-60fps for one) or paths to their manifests")
    ap.add_argument("--against", metavar="VERSION",
                    help="with --check-...: check against this release (e.g. v0.3.1, or v0.3.1-60fps for one "
                         "build) rather than the latest builds")
    if not (sys.argv[1:] if argv is None else argv):
        ap.print_help()
        sys.exit(2)
    args = ap.parse_args(argv)
    if args.diff and args.against:
        ap.error("--against only goes with --check-all-compat-patches and --check-compat-patch")

    if args.diff:
        for old, new in diff_pairs(ap, *args.diff):
            print_manifest_diff(old, new)
        return

    if args.against:
        targets = release_builds(ap, args.against)
    else:
        targets = {fps: manifests[-1][1] for fps in FPS_LIST if (manifests := release_manifests(fps))}
    against = args.against or "the latest builds"

    patch_arg, patch = args.check_compat_patch, None
    if patch_arg and patch_arg.endswith(".json"):
        patch = load_manifest(patch_arg)
        if patch is None:
            ap.error(f"manifest not found: {patch_arg}")
        if "mod_name" not in patch:
            ap.error(f"{patch_arg} is not a compat patch's manifest (those are in {COMPAT_MANIFEST_DIR})")
        if patch["fps"] not in targets:
            ap.error(f"no {patch['fps']} fps build of {against} to check against")
    elif patch_arg and not any(newest_compat_patches(load_manifest(target)["created_mod_version"], patch_arg)
                               for target in targets.values()):
        known = sorted({load_manifest(path)["mod_name"] for path in COMPAT_MANIFEST_DIR.glob("*.json")})
        if patch_arg.lower() in (name.lower() for name in known):
            ap.error(f"{patch_arg}'s compat patches are all for releases after {against}")
        ap.error(f"no compat patch manifests of {patch_arg}; there are some for: {', '.join(known) or 'no mods'}")

    if patch is not None:
        targets = {patch["fps"]: targets[patch["fps"]]}
    if not args.against:
        print("Checking against the latest builds: " + ", ".join(path.stem for path in targets.values()))
    if patch is not None:
        outdated = print_compat_status(targets[patch["fps"]], [(Path(patch_arg), patch)])
    else:
        outdated = sum(print_compat_status(target, mod_name=patch_arg) for target in targets.values())
    sys.exit(1 if outdated else 0)


def release_builds(ap, version_or_path) -> dict[int, Path]:
    """The release manifests a command-line argument names, by frame rate: a path, or a build's version
    (e.g. v0.3.1-60fps), names one; a release's version (e.g. v0.3.1) names each of its builds"""
    path = Path(version_or_path) if version_or_path.endswith(".json") else manifest_path(version_or_path)
    if path.is_file() or version_or_path.endswith(".json"):
        manifest = load_manifest(path)
        if manifest is None:
            ap.error(f"manifest not found: {path}")
        if "mod_name" in manifest:
            ap.error(f"{path} is a compat patch's manifest; check it with --check-compat-patch")
        return {manifest.get("fps", OLD_MANIFEST_FPS): path}
    builds = {fps: manifest_path(build_version(version_or_path, fps)) for fps in FPS_LIST}
    builds = {fps: path for fps, path in builds.items() if path.is_file()}
    if not builds:
        ap.error(f"no manifest of {normalize_version(version_or_path)} in {path.parent}")
    return builds


def diff_pairs(ap, old, new) -> list[tuple[Path, Path]]:
    """The (old, new) manifests to compare: the builds at each frame rate both releases have, or the
    two builds named, even at different frame rates"""
    old_builds, new_builds = release_builds(ap, old), release_builds(ap, new)
    if len(old_builds) == 1 and len(new_builds) == 1:
        return [(next(iter(old_builds.values())), next(iter(new_builds.values())))]
    pairs = [(old_builds[fps], new_builds[fps]) for fps in FPS_LIST if fps in old_builds and fps in new_builds]
    if not pairs:
        ap.error(f"{old} and {new} have no builds at the same frame rate; name the two builds to compare")
    return pairs
