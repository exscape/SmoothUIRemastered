"""Handles the settings menu's strings: assets/strings/en.csv, encoded into a .w3strings file for every
game language, in the mod's content folder in assets/mod_files/, which the main build ships from.
Every main build these from scratch, as they take next to no time; they aren't tracked.

Uses w3stringsr.exe: https://www.nexusmods.com/witcher3/mods/13094

First checks that every name in the settings menu's XML has a string (as the menu looks it up: see
assets/strings/en.csv), so that the menu never shows a raw key, and that the mod's script can give every
setting its default."""

import logging
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from ..common import fail, to_windows
from ..log import HEADING
from .shared import MOD_FILES, REPO_ROOT

log = logging.getLogger(__name__)

SOURCE = REPO_ROOT / "assets" / "strings" / "en.csv"
ID_SPACE = "13526"
# The languages the game has .w3strings files for (content/content0/*.w3strings)
LANGUAGES = ("ar", "br", "cn", "cz", "de", "en", "es", "esmx", "fr", "hu", "it", "jp", "kr", "pl", "ru", "tr",
             "ua", "zh")
LANGUAGE_META = re.compile(r"^;meta\[language=\w+\]$", re.MULTILINE)


def menu_xml(mod_name) -> Path:
    """The mod's settings menu"""
    return MOD_FILES / "bin" / "config" / "r4game" / "user_config_matrix" / "pc" / f"{mod_name}.xml"


def content_dir(mod_name) -> Path:
    """Where the mod's .w3strings files go (and are shipped from)"""
    return MOD_FILES / "mods" / mod_name / "content"


def check_tool(tool):
    """Fail unless tool (w3stringsr in [paths] of config.toml, as a Path) is a file"""
    if tool is None:
        fail("w3stringsr is not set in the [paths] section of config/config.toml; the main build needs it "
             "for the settings menu's strings")
    if not tool.is_file():
        fail(f"w3stringsr not found: {tool} (w3stringsr in the [paths] section of config/config.toml)")


def check_menu_strings(source: str, xml_path: Path):
    """Fail unless every displayName in the menu (and its presets' title) has a string in source; note
    unused strings"""
    xml = xml_path.read_text(encoding="utf-8")
    keys = {line.split("|")[2].lower() for line in source.splitlines()
            if line and not line.startswith(";") and line.count("|") >= 3}
    needed = {"panel_" + part for name in re.findall(r'<Group\b[^>]*\bdisplayName="([^"]+)"', xml)
              for part in name.split(".")}
    needed |= {"option_" + name for name in re.findall(r'<Var\b(?![^>]*hideAlways)[^>]*\bdisplayName="([^"]+)"', xml)}
    needed |= {"preset_value_" + name for name in re.findall(r'<(?:Option|Preset)\b[^>]*\bdisplayName="([^"]+)"', xml)}
    needed |= {"preset_" + name.replace(".", "_") for name in re.findall(
        r'<Group\b[^>]*\bdisplayName="([^"]+)"[^>]*>\s*(?:<!--.*?-->\s*)?<PresetsArray', xml, re.DOTALL)}
    needed = {key.lower() for key in needed} - {"panel_mods"}    # (the game's own)
    if missing := sorted(needed - keys):
        fail(f"{SOURCE.name} has no string for these keys of {xml_path.name}:\n  " + "\n  ".join(missing))
    # Optional: descriptions (see smoothui_settings.ws)
    descriptions = {key + "_description" for key in needed if key.startswith("option_")}
    if unused := sorted(keys - needed - descriptions):
        log.info(f"Note: strings that {xml_path.name} doesn't use: {', '.join(unused)}")


def check_menu_defaults(xml_path: Path, scripts: str):
    """Fail unless the mod's scripts (scripts: their text) can give every setting of the menu its default
    (see SmoothUI_InitOptions): each group has an id starting with SmoothUIRemastered and a hidden DefaultsSet,
    the visible settings' ids are unique among the groups, and every slider has a default in
    SmoothUI_SliderDefault (within its range, if it's a number)"""
    body = re.search(r"\bfunction SmoothUI_SliderDefault\b.*?\n}", scripts, re.DOTALL)
    defaults = dict(re.findall(r"\bcase '(\w+)':\s*return (.+?);", body[0])) if body else {}
    problems, ids, sliders = [], set(), set()
    for group in ET.parse(xml_path).getroot().iter("Group"):
        group_id = group.get("id")
        if not group_id.startswith("SmoothUIRemastered"):
            problems.append(f"group {group_id}: its id doesn't start with SmoothUIRemastered")
        if not any(var.get("id") == "DefaultsSet" and var.get("visibilityCondition") == "hideAlways"
                   for var in group.iter("Var")):
            problems.append(f"group {group_id}: has no hidden DefaultsSet")
        for var in group.iter("Var"):
            var_id, display_type = var.get("id"), var.get("displayType", "")
            if var.get("visibilityCondition") != "hideAlways":
                if var_id in ids:
                    problems.append(f"{var_id}: the id is in more than one group")
                ids.add(var_id)
            if not display_type.startswith("SLIDER"):
                continue
            sliders.add(var_id)
            slider = re.fullmatch(r"SLIDER;(-?[\d.]+);(-?[\d.]+);\d+", display_type)
            default = re.fullmatch(r'"(-?[\d.]+)"', defaults.get(var_id, ""))
            if not slider:
                problems.append(f"{var_id}: displayType isn't SLIDER;min;max;steps")
            elif var_id not in defaults:
                problems.append(f"{var_id}: has no default in SmoothUI_SliderDefault")
            elif default and not float(slider[1]) <= float(default[1]) <= float(slider[2]):
                problems.append(f"{var_id}: the default ({default[1]}) is outside {slider[1]}-{slider[2]}")
    problems += [f"{var_id}: has a default in SmoothUI_SliderDefault, but isn't a slider"
                 for var_id in sorted(defaults.keys() - sliders)]
    if problems:
        fail(f"{xml_path.name}: settings without a working default:\n  " + "\n  ".join(problems))


def build_strings(tool: Path | None, work_dir: Path, mod_name: str, dry_run=False):
    """Check the strings against the mod's settings menu, then (unless dry_run) replace the mod's .w3strings
    files with new ones; tool is w3stringsr, work_dir a folder for temporary files"""
    log.info("Building the settings menu's strings", extra=HEADING)
    source = SOURCE.read_text(encoding="utf-8")
    if len(LANGUAGE_META.findall(source)) != 1:
        fail(f"{SOURCE} should have one ;meta[language=..] line")
    check_menu_strings(source, menu_xml(mod_name))
    out_dir = content_dir(mod_name)
    check_menu_defaults(menu_xml(mod_name), "\n".join(script.read_text(encoding="utf-8")
                                                       for script in sorted(out_dir.rglob("*.ws"))))
    if dry_run:
        log.info(f"Dry run: would write {len(LANGUAGES)} .w3strings files to {out_dir.relative_to(REPO_ROOT)}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.w3strings"):
        old.unlink()
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        for language in LANGUAGES:
            csv = Path(tmp) / f"{language}.csv"
            csv.write_text(LANGUAGE_META.sub(f";meta[language={language}]", source), encoding="utf-8")
            out = out_dir / f"{language}.w3strings"
            result = subprocess.run([str(tool), "--id-space", ID_SPACE, to_windows(csv), to_windows(out)],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                    check=False)
            if result.returncode != 0 or not out.is_file():
                fail(f"w3stringsr failed for {language} (exit code {result.returncode}):\n"
                     f"{(result.stdout + result.stderr).strip()}")
    log.info(f"Wrote {len(LANGUAGES)} .w3strings files to {out_dir.relative_to(REPO_ROOT)}")
