#!/usr/bin/env python3

# build-compat.py -- take an existing mod .zip/.rar/.7z, apply framerate patches to the .redswf files
# that conflict with Smooth UI Remastered, and build a compatibility patch containing only
# those patched files.
#
# The files to patch and their settings come from the manifest (see manifests/), selected by --smooth-version,
# optionally with additions from an override file (--overrides; see config/compat_overrides_example.txt).
# Example usage: python build-compat.py modOtherUI.zip --mod-name OtherMod --mod-version 1.0 --smooth-version 0.2

import argparse
import shutil
import zipfile
from functools import partial
from pathlib import Path

import py7zr
import rarfile

from ..common import fail, normalize_version, print_step_header, to_windows
from ..patcher import dev_mode, patcher_core
from ..patcher.fixes import check_fix_name
from ..patcher.patcher_core import PatchSettings
from ..patcher.swf_patcher import FpsRule, SpriteRef, parse_sprite_pattern
from .profile import apply_overrides
from .reviewed_warnings import WarningReview, print_warning_summary, review_of
from .shared import (
    REPO_ROOT,
    config_paths,
    enforce_strict,
    finish_work_dir,
    install_and_launch,
    load_config,
    load_manifest,
    pack_content,
    print_file_result,
    print_rule_issues,
    resolve_zip_path,
    wcc_lite,
    zip_tree,
)


# rarfile looks for an external unrar in PATH and in the current directory. When neither has it,
# point it at the UnRAR.exe bundled in this repo, so .rar mods work from any working directory.
def setup_rar_tool():
    if shutil.which(rarfile.UNRAR_TOOL):
        return # a system unrar is preferred (e.g. under WSL)
    bundled_unrar = REPO_ROOT / "UnRAR.exe"
    if bundled_unrar.is_file():
        rarfile.UNRAR_TOOL = str(bundled_unrar)

setup_rar_tool()

# Used to name the generated mod. The "mod000_" prefix sorts before the main mod and
# (nearly) any other mod, so the compatibility patch loads first and wins conflicts.
MAIN_MOD_NAME = "SmoothUIRemastered"

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Build a Smooth UI compatibility patch for an existing mod .zip/.rar/.7z")

    ap.add_argument("mod_archive", type=Path,
                    help="the other mod's .zip/.rar/.7z (must contain blob*.bundle)")
    ap.add_argument("--mod-name", required=True,
                    help="name of the other mod; only used to name the generated patch file")
    ap.add_argument("--mod-version", required=True,
                    help="version of the other mod; only used to name the generated patch file")
    ap.add_argument("--smooth-version", required=True,
                    help="Smooth UI Remastered version that the patch targets; affects the patching process")
    ap.add_argument("--overrides", type=Path, default=None,
                    help="optional file with settings to add for this mod, on top of those the Smooth UI "
                         "release was patched with (see config/compat_overrides_example.txt)")

    ap.add_argument("--zip", default=None,
                    help="output .zip name or path; the default name is recommended over specifying this!")
    ap.add_argument("--install", action="store_true",
                    help="install the new mod into the game folder on success")
    ap.add_argument("--launch", action="store_true",
                    help="launch the game after build and install success")
    ap.add_argument("--strict", action=argparse.BooleanOptionalAction, default=True,
                    help="fail unless every file is patched exactly as configured: no file failed or left "
                         "unchanged, every explicit rule followed, and no unreviewed warnings (default: on; "
                         "use --no-strict to build anyway, e.g. while experimenting)")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the work directory on success (it is always kept on failure)")

    # Dev hooks; useful for exploring the data format, etc.
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dev-dump", metavar="FILE",
                   help="DEV: target a specific .redswf FILE, write its decompressed body to "
                        "{FILE}_payload.swf, and exit")
    g.add_argument("--dev-insert", metavar="FILE",
                   help="DEV: target a specific .redswf FILE, and replace its body with the "
                        "(manually edited) contents of {FILE}_payload.swf")
    g.add_argument("--dev-pause", metavar="FILE",
                   help="DEV: target a specific .redswf FILE, dump its body to {FILE}_payload.swf, "
                        "pause until you type 'continue', then use the edited file")

    return ap.parse_args(argv)

def mod_folder_name(name):
    return f"mod000_{MAIN_MOD_NAME}_{name}_Compat"

def check_member_paths(names, dest_dir):
    """Refuse archives containing paths that would extract outside dest_dir"""
    root = dest_dir.resolve()
    for name in names:
        target = (root / name).resolve()

        if target != root and root not in target.parents:
            fail(f"unsafe path in archive: {name}")

def unpack(mod_archive, dest_dir):
    """Extract the whole mod archive (.zip, .7z or .rar) into dest_dir"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        if zipfile.is_zipfile(mod_archive):
            with zipfile.ZipFile(mod_archive) as z:
                check_member_paths(z.namelist(), dest_dir)
                z.extractall(dest_dir)
        elif py7zr.is_7zfile(mod_archive):
            with py7zr.SevenZipFile(mod_archive) as z:
                check_member_paths(z.getnames(), dest_dir)
                z.extractall(path=dest_dir)
        elif rarfile.is_rarfile(mod_archive):
            with rarfile.RarFile(mod_archive) as z:
                check_member_paths(z.namelist(), dest_dir)
                z.extractall(dest_dir)
        else:
            fail(f"not a supported archive (.zip, .7z, .rar): {mod_archive}")
    except rarfile.RarCannotExec:
        fail("unpacking .rar files needs an external tool: install UnRAR, unar or bsdtar "
             "and make sure it is on your PATH. See README.md for more information.")
    except (zipfile.BadZipFile, py7zr.Bad7zFile, rarfile.Error) as e:
        fail(f"could not unpack {mod_archive}: {e}")
    count = sum(1 for f in dest_dir.rglob("*") if f.is_file())
    print(f"Unpacked {count} files into {dest_dir}")

def is_dlc_dir(rel):
    """Whether a folder holding bundles (relative to the unpacked archive) is DLC content: anything
    under a dlc folder, or a dlcXxx/content folder shipped without the dlc folder around it"""
    return "dlc" in (part.lower() for part in rel.parts) or rel.parent.name.lower().startswith("dlc")

def find_unbundle_root(unpacked_dir):
    """The directory to give to wcc_lite unbundle: the modXxx folder if there is one,
    otherwise the folder that holds the bundle(s).

    DLC bundles are skipped, like the mod's loose files: they never conflict with the files
    Smooth UI Remastered patches, so only the bundles in a mod folder matter."""
    content_dirs = sorted({b.parent for b in unpacked_dir.rglob("*.bundle")})
    if not content_dirs:
        fail("no .bundle files found in the .zip (mods shipping loose files are not supported)")

    dlc_dirs = [d for d in content_dirs if is_dlc_dir(d.relative_to(unpacked_dir))]
    for d in dlc_dirs:
        print(f"  ignored: {d.relative_to(unpacked_dir)} (DLC bundles can't conflict with Smooth UI Remastered)")
    content_dirs = [d for d in content_dirs if d not in dlc_dirs]

    if not content_dirs:
        fail("only DLC bundles found, none in a mod folder. This mod does not need a compat patch.")
    if len(content_dirs) > 1:
        # Usually a mod with optional parts. One patch covering every part would override the files
        # of parts the user didn't install, and parts may ship different versions of the same file.
        listing = "\n".join(f"  {d.relative_to(unpacked_dir)}" for d in content_dirs)
        fail(f"bundles found in several mod folders, this is not supported:\n{listing}\n"
             "If these are optional parts, build from an archive holding only the part to support.")
    content = content_dirs[0]
    if content.name.lower() == "content" and content.parent.name.lower().startswith("mod"):
        return content.parent
    return content

def unbundle(root, out_dir):
    """Extract the bundle(s) under root into loose files under out_dir, at their depot paths"""
    out_dir.mkdir(parents=True, exist_ok=True)
    wcc_lite("unbundle", f"-dir={to_windows(root)}", f"-outdir={to_windows(out_dir)}")
    if not any(out_dir.rglob("*.redswf")):
        fail(f"no .redswf files found after unbundling into {out_dir}")

def profile_from_manifest(manifest):
    """The settings each file was patched with in a Smooth UI release, from its manifest: a profile
    ({profile key: PatchSettings}), so compat patches match the release.

    Sprite ids carry the class and frame count the release recorded for them, so that they aren't
    applied to a different sprite in the other mod's version of the file. Manifests from before
    v0.3 have no reports, and only give the frame rate; ones from before fixes existed have none."""
    profile = {}
    for key, record in manifest["files"].items():
        report = record.get("report")
        if report is None:
            profile[key] = PatchSettings(float(record["fps"]))
            continue
        profile[key] = PatchSettings(float(record["fps"]),
                                     tuple(FpsRule(rule["pattern"], rule["fps"]) for rule in report["rules"]),
                                     _recorded_tokens(report, "include"), _recorded_tokens(report, "exclude"),
                                     _recorded_fixes(key, report))
    return profile

def _recorded_fixes(key, report):
    """The names of the fixes a release applied to a file"""
    names = tuple(fix["name"] for fix in report.get("fixes", []))
    for name in names:
        try:
            check_fix_name(name)
        except ValueError as e:
            fail(f"{key}: the release's manifest names {e}; this version of the patcher can't reproduce it")
    return names

def _recorded_tokens(report, kind):
    """A report's include or exclude patterns, with sprite ids expecting the class and frame count
    recorded for them"""
    recorded_sprites = {sprite["id"]: (sprite["class"], sprite["frames"]) for sprite in report["sprites"]}
    parsed = (parse_sprite_pattern(setting["pattern"]) for setting in report[kind])
    return tuple(SpriteRef(token.id, recorded_sprites.get(token.id)) if isinstance(token, SpriteRef)
                 else token for token in parsed)

def find_shared_files(their_dir, manifest, smooth_version):
    """The .redswf files (paths relative to their_dir, in their spelling) that the other mod
    ships AND that the Smooth UI Remastered release named by manifest patches"""
    def key(rel):
        """One comparable spelling for both sides: lowercase, slash-separated"""
        return rel.as_posix().lower() if isinstance(rel, Path) else rel.lower()

    if manifest is None:
        fail(f"no manifest exists for Smooth UI Remastered {smooth_version}")

    our_keys = {key(rel) for rel in manifest["files"]}
    theirs = sorted(f.relative_to(their_dir) for f in their_dir.rglob("*.redswf"))
    shared = [p for p in theirs if key(p) in our_keys]

    print(f"The other mod has {len(theirs)} .redswf files, {len(shared)} of which "
          f"Smooth UI Remastered {smooth_version} also patches")
    for p in theirs:
        if key(p) not in our_keys:
            print(f"  not patched: {p} (not in our mod)")

    if not shared:
        fail("no .redswf files in common! This mod does not need a compat patch.")
    return shared

def main():
    args = parse_args()

    # Dev mode: hand the mode to the patcher's dev hook
    if args.dev_dump:
        dev_mode.set_dev("dump", args.dev_dump)
    elif args.dev_insert:
        dev_mode.set_dev("insert", args.dev_insert)
    elif args.dev_pause:
        dev_mode.set_dev("pause", args.dev_pause)

    if not args.mod_archive.is_file():
        fail(f"input mod archive not found: {args.mod_archive}")

    paths = config_paths(load_config())
    game_path = paths.get("game_path")

    mod_name = mod_folder_name(args.mod_name)
    mod_version = normalize_version(args.mod_version)
    smooth_version = normalize_version(args.smooth_version)

    zip_out = resolve_zip_path(args.zip, f"SmoothUIRemastered_{smooth_version}_{args.mod_name}_{mod_version}_Compat.zip")

    work_dir = paths["working_dir"] / "compat"
    unpacked_dir = work_dir / "1_unpacked"
    unbundled_dir = work_dir / "2_unbundled"
    patched_dir = work_dir / "3_patched"
    mod_root = work_dir / "4_mod"
    mod_dir = mod_root / mod_name
    content_dir = mod_dir / "content"

    # Perform some sanity checks, since every user that didn't change the config will likely get a very, very ugly exception tree otherwise
    if game_path and not game_path.is_dir():
        fail(f"game_path is not set to a valid directory in config/config.toml: {game_path} is not an existing directory")
    if not (parent := work_dir.parent.parent).is_dir():
        fail(f"working directory parent {parent} does not exist")
    if not paths["wcc_lite"].is_file():
        fail(f"wcc_lite setting in config/config.toml is not set to a valid wcc_lite.exe path: {paths['wcc_lite']} does not exist")

    print(f"Mod folder: {mod_name}")
    print(f"Output ZIP: {zip_out.name}")

    print_step_header(f"Cleaning {work_dir}")
    shutil.rmtree(work_dir, ignore_errors=True)
    content_dir.mkdir(parents=True)

    print_step_header(f"Unpacking {args.mod_archive}")
    unpack(args.mod_archive, unpacked_dir)

    print_step_header("Unbundling the other mod")
    unbundle(find_unbundle_root(unpacked_dir), unbundled_dir)

    print_step_header(f"Loading manifest for Smooth UI Remastered {smooth_version}")
    manifest = load_manifest(smooth_version)

    print_step_header("Finding files in common")
    shared = find_shared_files(unbundled_dir, manifest, smooth_version)
    shared_keys = {rel.as_posix().lower() for rel in shared}
    shared_profile = {key: settings for key, settings in profile_from_manifest(manifest).items()
                      if key in shared_keys}
    shared_settings, reviewed = [], set()
    if args.overrides:
        shared_profile, shared_settings, reviewed = apply_overrides(shared_profile, args.overrides)

    print_step_header("Running FPS patcher")

    # The release's reports; warnings it had for a file were already there in the vanilla version.
    # They stay hidden where the other mod's code is the same (see reviewed_warnings.py).
    recorded = {key: record["report"] for key, record in manifest["files"].items() if "report" in record}
    review = WarningReview(reviewed, {key: {review_of(warning) for warning in report["warnings"]}
                                      for key, report in recorded.items()})
    try:
        patched, kept, failed = patcher_core.patch_files(
            unbundled_dir, patched_dir, shared_profile,
            on_file=partial(print_file_result, is_reviewed=review.is_reviewed))
    except (NotADirectoryError, ValueError) as e:
        fail(str(e))
    print()
    print(f"Done: {len(patched)} patched, {len(kept)} left alone, {len(failed)} failed.")

    warnings = review.summarize([*patched, *kept], unchecked=len(failed))
    how_to_accept = [("These come from the other mod's changes. Once checked, add these lines under * in "
                      "the --overrides file to mark them as reviewed:"),
                     *(f"    reviewed: {review}" for review in warnings.unreviewed)]
    print_warnings = partial(print_warning_summary, warnings, how_to_accept=how_to_accept,
                             reviewed_source=str(args.overrides))
    print_warnings()
    rule_issues = patcher_core.find_rule_issues(patched, kept, shared_settings, recorded)
    print_rule_issues(rule_issues)
    if args.strict:
        enforce_strict(failed, kept, rule_issues.errors, warnings.unreviewed)

    # A conflicting file missing from the patch would let our main mod's version
    # override the other mod's changes to that screen.
    if failed:
        fail(f"{len(failed)} conflicting file(s) could not be patched, see above")
    if not patched:
        fail("no patched files were produced, aborting.")

    pack_content(patched_dir, content_dir)

    zip_tree(mod_root, zip_out)

    install_and_launch(mod_dir, game_path, args.install, args.launch)

    finish_work_dir(work_dir, args.keep_work)

    # Repeated, as the packing output has likely scrolled it away
    print_warnings()
    print_rule_issues(rule_issues)