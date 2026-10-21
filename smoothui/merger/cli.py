#!/usr/bin/env python3

# merge.py -- the merger's command line. --scan (read-only) shows which Smooth UI build is installed, which
# mod's copy of each file it patches the game loads, and what keeps mods or files from being patched.

import argparse
import logging
import time
from pathlib import Path

from ..build.shared import config_paths, load_config
from ..common import fail, to_native
from ..log import HEADING
from ..witcher3.user_files import mods_settings_path
from .installed import is_smoothui_folder
from .load_order import Group
from .scan import scan

log = logging.getLogger(__name__)


def parse_args():
    ap = argparse.ArgumentParser(description="Smooth UI Remastered merger: patch other mods' copies of the files "
                                             "Smooth UI patches")
    ap.add_argument("--scan", action="store_true", required=True,
                    help="show the installed Smooth UI build, which mod's copy of each file it patches the game "
                         "loads, and any problems; changes nothing (the only mode for now)")
    ap.add_argument("--game-path", type=to_native, default=None,
                    help="the game folder (default: game_path in config/config.toml)")
    ap.add_argument("--mods-settings", type=to_native, default=None,
                    help=r"the mods.settings file (default: the one in Documents\The Witcher 3)")
    return ap.parse_args()


def main():
    args = parse_args()
    game_path = args.game_path or config_paths(load_config()).get("game_path")
    if game_path is None:
        fail("no game folder: give --game-path, or set game_path in config/config.toml")
    mods_settings = args.mods_settings or mods_settings_path()

    start = time.perf_counter()
    result = scan(game_path, mods_settings)
    elapsed = time.perf_counter() - start

    log.info("Installed Smooth UI Remastered", extra=HEADING)
    if not result.installed.usable:
        fail(result.installed.message)
    log.info(result.installed.message)

    log.info("Load order of the mods that ship a file Smooth UI patches", extra=HEADING)
    relevant = {mod for file in result.files.values() for mod in file.shippers}
    for position, mod in enumerate(result.order, 1):
        if mod in relevant or is_smoothui_folder(mod.folder):
            log.info(f"{position:4}. {mod.folder} ({_entry(mod)})")

    log.info("The copy each file is patched from (and the one the game loads now, if another)", extra=HEADING)
    for key, file in result.files.items():
        loaded = file.loaded.folder if file.loaded else None
        if file.undetermined:
            log.warning(f"{key}: can't be determined ({', '.join(mod.folder for mod in file.undetermined)})")
            continue
        if file.winner:
            text = f"{key}: {file.winner.origin} ({Path(file.winner.file).name})"
            source = file.winner.origin
        else:
            text, source = f"{key}: Smooth UI's own copy", result.installed.folder
        if loaded and loaded != source:
            text += f"; loaded now: {loaded}"
        # Smooth UI ships every file
        others = [mod.folder for mod in file.shippers
                  if mod.folder not in (loaded, source) and not is_smoothui_folder(mod.folder)]
        log.info(text + (f"; also shipped by {', '.join(others)}" if others else ""))

    log.info("Problems", extra=HEADING)
    for problem in result.problems:
        log.warning(problem.message)
    if not result.problems:
        log.info("None")
    log.info(f"\nScanned in {elapsed:.2f} s")


def _entry(mod):
    """A mod's mods.settings entry, for the load order listing"""
    if mod.group != Group.WITH_SECTION:
        return "no mods.settings entry"
    return f"Priority {mod.priority}" if mod.priority is not None else "mods.settings entry without a Priority"
