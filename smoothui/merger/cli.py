#!/usr/bin/env python3

# merge.py -- the merger's command line, for development and tests: a thin layer over the library (see
# builder.py). --scan (read-only) shows which Smooth UI build is installed, which mod's copy of each file it
# patches the game loads, whether the package is up to date (see state.py), and what keeps mods or files
# from being patched; it runs the launcher's check, and exits with code 2 when the launcher would open the
# merger. --build builds the package. --check-updates runs the window's update check (see updates.py).

import argparse
import logging
import os
import sys
import time

from ..build.shared import config_paths, load_config
from ..common import fail, to_native
from ..log import HEADING, SUBHEADING
from ..paths import DATA_DIR
from ..witcher3.user_files import mods_settings_path
from . import updates
from .builder import build_package
from .game import groups_off, load_settings
from .installed import is_smoothui_folder
from .load_order import Group
from .scan import is_package_folder
from .screens import screen_label
from .state import State, check

log = logging.getLogger(__name__)

LEVELS = {"ok": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def parse_args():
    ap = argparse.ArgumentParser(description="Smooth UI Remastered merger: patch other mods' copies of the files "
                                             "Smooth UI patches (for development and tests)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true",
                      help="show the installed Smooth UI build, which mod's copy of each file it patches the game "
                           "loads, any problems, and what the launcher would do (exit code 2: open the merger); "
                           "changes nothing")
    mode.add_argument("--build", action="store_true",
                      help="build the package into the game's Mods folder, and give it its mods.settings entry")
    mode.add_argument("--check-updates", action="store_true",
                      help="ask GitHub (or SMOOTHUI_UPDATE_URL) for a newer Smooth UI Tools release, and download "
                           "new release manifests into --data-dir (even if the last check was just now)")
    ap.add_argument("--game-path", type=to_native, default=None,
                    help="the game folder (default: game_path in config/config.toml)")
    ap.add_argument("--mods-settings", type=to_native, default=None,
                    help=r"the mods.settings file (default: the one in Documents\The Witcher 3)")
    ap.add_argument("--out", type=to_native, default=None,
                    help="--build: write the package into this folder instead, and leave mods.settings alone")
    ap.add_argument("--data-dir", type=to_native, default=DATA_DIR,
                    help="the merger's data folder, with the mods.settings backup and the records of packages "
                         r"that hold no files (default: data\ in the repo); use another one for test setups")
    ap.add_argument("--force", action="append", default=[], metavar="FILE",
                    help="--build: put this file (e.g. gameplay/gui_new/swf/hud/hud_minimap2.redswf) in the package "
                         "even though its rescale had problems; repeatable")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.check_updates:
        show_updates(args.data_dir)
        return
    game_path = args.game_path or config_paths(load_config()).get("game_path")
    if game_path is None:
        fail("no game folder: give --game-path, or set game_path in config/config.toml")
    mods_settings = args.mods_settings or mods_settings_path()
    if args.build:
        build(args, game_path, mods_settings)
    else:
        show_scan(game_path, mods_settings, args.data_dir)


def show_scan(game_path, mods_settings, data_dir):
    start = time.perf_counter()
    found = check(game_path, mods_settings, data_dir=data_dir, groups_off=_groups_off(data_dir))
    elapsed = time.perf_counter() - start
    result = found.scan

    log.info("Installed Smooth UI Remastered", extra=HEADING)
    if not result.installed.usable:
        log.warning(result.installed.message)
        if found.package_without_smoothui:
            log.warning("The merged patches are still installed: they replace screens with copies patched for "
                        "another Smooth UI build, or for none")
        if result.conflicts:
            log.warning(f"Other mods change screens Smooth UI changes, so some may look wrong: "
                        f"{', '.join(result.conflicts)}")
        _show_verdict(found, elapsed)
        return
    log.info(result.installed.message)

    log.info("Load order of the mods that change a screen Smooth UI patches", extra=HEADING)
    relevant = {mod for file in result.files.values() for mod in file.shippers}
    for position, mod in enumerate(result.order, 1):
        if mod in relevant or is_smoothui_folder(mod.folder):
            log.info(f"{position:4}. {mod.folder} ({_entry(mod)})")

    log.info("Screens: what the merged patches hold, and what merging would change", extra=HEADING)
    for state in found.states:
        file = result.files[state.path]
        loaded = file.loaded.folder if file.loaded else None
        severity = f" ({state.severity.name.lower()})" if state.severity else ""
        text = f"{screen_label(state.path)}: {state.state.value}{severity}: {state.message}"
        if loaded and state.needs_build and loaded != state.winner:
            text += f"; loaded now: {loaded}"
        # Smooth UI ships every file
        others = [mod.folder for mod in file.shippers if mod.folder not in (loaded, state.winner)
                  and not is_smoothui_folder(mod.folder) and not is_package_folder(mod.folder)]
        attention = state.severity is not None or state.state == State.UNDETERMINED
        log.log(logging.WARNING if attention else logging.INFO,
                text + (f"; also changed by {', '.join(others)}" if others else ""))
    to_build = len(found.needs_build)
    log.info(f"\nMerging is needed: it would change {to_build} screen(s)" if to_build else "\nNo merge is needed")

    _show_problems(result.problems, [f"{folder} is a Smooth UI compat patch: the merged patches replace it for every "
                                     "screen they hold" for folder in result.compat_patches])
    _show_verdict(found, elapsed)


def _show_verdict(found, elapsed):
    """What the launcher would do (see state.Check), and the time the check took; exit code 2 if it would
    open the merger"""
    log.info("Launcher", extra=HEADING)
    if found.stale:
        log.warning(f"The launcher would open the merger (playing without merging: {found.severity.name.lower()})")
    else:
        log.info("The launcher would start the game")
    log.info(f"\nChecked in {elapsed:.2f} s")
    if found.stale:
        sys.exit(2)


def show_updates(data_dir):
    log.info("Update check", extra=HEADING)
    test = f" (from {updates.UPDATE_URL_ENV})" if os.environ.get(updates.UPDATE_URL_ENV) else ""
    if updates.token():
        test += f", with {updates.UPDATE_TOKEN_ENV}"
    log.info(f"Release list: {updates.releases_url()}{test}")
    found = updates.check(data_dir)
    if found is None:
        fail("the check failed: see above")
    if found.update:
        log.info(f"Download: {found.update.asset.url} ({found.update.asset.size} bytes)")
        for version, notes in found.update.notes:
            log.info(f"Smooth UI Tools v{version}", extra=SUBHEADING)
            log.info(notes.strip() or "(no release notes)")
    log.info(f"\nManifests downloaded into {data_dir / updates.MANIFESTS_DIR}: {', '.join(found.downloaded) or 'none'}")


def build(args, game_path, mods_settings):
    start = time.perf_counter()
    result = build_package(game_path, mods_settings, force=args.force, out_dir=args.out, data_dir=args.data_dir,
                           groups_off=_groups_off(args.data_dir))
    show_build(result)
    log.info(f"\nMerged in {time.perf_counter() - start:.2f} s" + ("" if result.clean else "; see the problems above"))
    if not result.clean:
        sys.exit(2)


def _groups_off(data_dir):
    """The groups turned off (see game.groups_off) in the merger's settings in data_dir (none if it has none)"""
    return groups_off(load_settings(data_dir) if data_dir is not None else None)


def show_build(result):
    """Log what build_package did"""
    log.info("Installed Smooth UI Remastered", extra=HEADING)
    log.info(result.scan.installed.message)
    log.info("Screens", extra=HEADING)
    for file in result.files:
        log.log(LEVELS[file.severity], file.explain())
    if not result.files:
        log.info("No other mod changes a screen Smooth UI patches")
    _show_problems(result.problems)

    log.info("Merged patches", extra=HEADING)
    installation = result.installation
    held = sum(file.status.included for file in result.files)
    if not installation.written.empty:
        log.info(f"Wrote {installation.package_dir} ({held} screen(s))")
    elif installation.written.removed:
        log.info(f"Nothing to merge, so there are no merged patches ({installation.package_dir} removed, if it was "
                 "there); mods.settings left as it is")
    else:
        log.warning(f"Nothing to merge, but {installation.package_dir} holds files the merger didn't write, so it "
                    "stays")
    for name in installation.written.unknown_files:
        log.warning(f"{name} in the merged patches' folder wasn't written by the merger: remove it")
    if installation.entry_changed is not None:
        log.info(f"mods.settings: {'entry written' if installation.entry_changed else 'entry already as needed'}")
    for problem in result.check_problems:
        log.error(problem)
    for note in result.notes:
        log.info(note)


def _show_problems(problems, notes=()):
    """Log the scan's problems (see scan.Problem), then notes (warnings that aren't problems)"""
    log.info("Problems", extra=HEADING)
    for message in [problem.message for problem in problems] + list(notes):
        log.warning(message)
    if not problems and not notes:
        log.info("None")


def _entry(mod):
    """A mod's mods.settings entry, for the load order listing"""
    if mod.group != Group.WITH_SECTION:
        return "no mods.settings entry"
    return f"Priority {mod.priority}" if mod.priority is not None else "mods.settings entry without a Priority"
