#!/usr/bin/env python3
"""The scripted steps of a release (dev/docs/release-checklist.md), run from the repo's root in WSL:

    ./release-helper.py tools-packages      Smooth UI Tools: update env_gui's packages, write
                                            requirements_gui-lock.txt and show what changed
                                            since the last published tools release
    ./release-helper.py tools-notes         Smooth UI Tools: check the release notes of config.toml's
                                            tools_version and show them
    ./release-helper.py tools-download      Smooth UI Tools: download the draft release's zip and notes to
                                            releases/tools/, and check the zip's proof (attestation)
    ./release-helper.py upload-manifests    Smooth UI: upload the manifests that the manifests release lacks
                                            (not dev builds, nor v0.3.3 and older: the tools ship those)

The tools' version is always tools_version in config/config.toml.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from smoothui.common import normalize_version
from smoothui.manifests import is_dev_version, version_key

ROOT = Path(__file__).resolve().parent
REPO = "exscape/SmoothUIRemastered"
CONFIG = ROOT / "config" / "config.toml"
ENV_GUI_PYTHON = ROOT / "env_gui" / "Scripts" / "python.exe"
REQUIREMENTS = "requirements_gui.txt"
LOCK = ROOT / "requirements_gui-lock.txt"
LOCK_COMMENT_LINES = 2
# Relative to ROOT, so that the commands shown are short
NOTES = Path("build") / "release-notes.md"
TOOLS_RELEASES = Path("releases") / "tools"
MANIFESTS = Path("manifests")
MANIFESTS_TAG = "manifests"
TOOLS_TAG = re.compile(r"tools-v([0-9]+)\.([0-9]+)\.([0-9]+)")
# The oldest public tools ship the manifests up to this version, so these needn't be uploaded
NEWEST_SHIPPED_MANIFEST = "v0.3.3"


def run(command, **kwargs):
    """Run a command, showing it first; stop if it fails"""
    print("$", " ".join(str(c).removeprefix(f"{ROOT}/") for c in command), flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False, **kwargs)
    if result.returncode != 0:
        sys.exit(f"ERROR: that command failed (exit code {result.returncode})")
    return result


def heading(text):
    print(f"\n=== {text} ===", flush=True)


def config_version(key):
    with open(CONFIG, "rb") as f:
        version = tomllib.load(f).get("versions", {}).get(key)
    if not version:
        sys.exit(f"ERROR: no {key} in {CONFIG.relative_to(ROOT)}")
    return version


def last_tools_release():
    """The tag and commit of the newest published tools release on GitHub. Not from the local tags: a tag may
    not have been released (a failed build, or a draft)."""
    releases = json.loads(run(["gh", "release", "list", "--repo", REPO, "--exclude-drafts", "--exclude-pre-releases",
                               "--limit", "100", "--json", "tagName"], capture_output=True, text=True).stdout)
    versions = {tuple(map(int, match.groups())): match[0]
                for release in releases if (match := TOOLS_TAG.fullmatch(release["tagName"]))}
    if not versions:
        sys.exit("ERROR: GitHub has no published tools release")
    tag = versions[max(versions)]
    commit = run(["gh", "api", f"repos/{REPO}/commits/{tag}", "--jq", ".sha"],
                 capture_output=True, text=True).stdout.strip()
    if subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=ROOT, check=False).returncode != 0:
        sys.exit(f"ERROR: {tag}'s commit {commit} isn't in this repo: git fetch first")
    return tag, commit


def tools_packages(args):
    if not ENV_GUI_PYTHON.is_file():
        sys.exit(f"ERROR: no {ENV_GUI_PYTHON.relative_to(ROOT)} (see {REQUIREMENTS})")
    # First, so that a failure here doesn't come after the long install
    heading("Finding the last published tools release")
    tag, commit = last_tools_release()
    heading(f"Updating env_gui from {REQUIREMENTS}")
    run([ENV_GUI_PYTHON, "-m", "pip", "install", "-U", "-r", REQUIREMENTS])

    heading(f"Writing {LOCK.name}")
    freeze = run([ENV_GUI_PYTHON, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
    comment = LOCK.read_text(encoding="utf-8").splitlines(keepends=True)[:LOCK_COMMENT_LINES]
    LOCK.write_text("".join(comment) + freeze.replace("\r", ""), encoding="utf-8", newline="\n")

    heading(f"{LOCK.name} compared to the last release, {tag}")
    diff = run(["git", "--no-pager", "diff", "--color=always", commit, "--", LOCK.name], capture_output=True,
               text=True)
    if diff.stdout.strip():
        print(diff.stdout, end="")
    else:
        print(f"No changes: {LOCK.name} is the same as in {tag}.")


def tools_notes(args):
    version = config_version("tools_version")
    heading(f"Checking the notes of Smooth UI Tools v{version}")
    run([sys.executable, "dev-tools/release-notes.py", version, NOTES])
    notes = (ROOT / NOTES).read_text(encoding="utf-8")
    heading(f"{NOTES} (the release's text)")
    print(notes, end="" if notes.endswith("\n") else "\n")
    # Imported here: only this needs the merger's packages
    from smoothui.merger.updates import CRITICAL
    print()
    if CRITICAL.search(notes):
        print("CRITICAL: older versions will show this as a critical update (a line starts with 'Critical fix:').")
    else:
        print("Not critical: older versions will show a normal update notice.")


def tools_download(args):
    version = config_version("tools_version")
    tag = f"tools-v{version}"
    zip_path = TOOLS_RELEASES / f"SmoothUITools-{version}.zip"
    notes_path = zip_path.with_suffix(".md")
    if (ROOT / zip_path).exists():
        sys.exit(f"ERROR: {zip_path} already exists: delete it first if it should be replaced")

    heading(f"Downloading the draft release {tag}")
    (ROOT / TOOLS_RELEASES).mkdir(parents=True, exist_ok=True)
    run(["gh", "release", "download", tag, "--repo", REPO, "--pattern", zip_path.name, "--dir", TOOLS_RELEASES])
    body = run(["gh", "release", "view", tag, "--repo", REPO, "--json", "body", "--jq", ".body"],
               capture_output=True, text=True).stdout
    # GitHub keeps the text with Windows line ends
    (ROOT / notes_path).write_text(body.replace("\r", "").rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {notes_path}: the release's text")

    heading("Checking the zip's proof (that GitHub's workflow built it)")
    run(["gh", "attestation", "verify", zip_path, "-R", REPO])

    heading("Done")
    print(f"Keep both in {TOOLS_RELEASES}/ (the next release's update test needs them):")
    print(f"  {zip_path}\n  {notes_path}")


def is_manifest_to_upload(path):
    """A manifest the merger downloads (<version>.json of a release, not of a dev build) that the oldest public
    tools don't ship"""
    version = path.stem
    return (path.suffix == ".json" and "dev" not in path.name and normalize_version(version) == version
            and version_key(version) is not None and not is_dev_version(version)
            and version_key(version) > version_key(NEWEST_SHIPPED_MANIFEST))


def upload_manifests(args):
    heading(f"Comparing {MANIFESTS}/ with the {MANIFESTS_TAG} release")
    assets = json.loads(run(["gh", "release", "view", MANIFESTS_TAG, "--repo", REPO, "--json", "assets"],
                            capture_output=True, text=True).stdout)["assets"]
    digests = {asset["name"]: asset.get("digest") for asset in assets}
    new, changed = [], []
    for path in sorted((ROOT / MANIFESTS).glob("*.json")):
        if not is_manifest_to_upload(path):
            continue
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if path.name not in digests:
            new.append(path)
        elif digests[path.name] != digest:
            changed.append(path)

    if changed and not args.replace:
        print("Different from the uploaded copy (corrected manifests?); --replace uploads them:")
        print("".join(f"  {path.name}\n" for path in changed), end="")
        changed = []
    upload = new + changed
    if not upload:
        print(f"Nothing to upload: the release has every manifest newer than {NEWEST_SHIPPED_MANIFEST}.")
        return
    print("These manifests would be uploaded:")
    for path in upload:
        print(f"  {path.name}" + ("  (replaces the uploaded copy)" if path in changed else ""))
    if input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("Nothing uploaded.")
    run(["gh", "release", "upload", MANIFESTS_TAG, "--repo", REPO, *(["--clobber"] if changed else []),
         *(MANIFESTS / path.name for path in upload)])
    print("Uploaded. Installed tools download them at their next update check.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("tools-packages", help="update env_gui's packages and write requirements_gui-lock.txt")
    commands.add_parser("tools-notes", help="check and show the tools release's notes")
    commands.add_parser("tools-download", help="download the draft tools release to releases/tools/")
    upload = commands.add_parser("upload-manifests", help="upload the manifests the manifests release lacks")
    upload.add_argument("--replace", action="store_true",
                        help="also upload manifests that differ from the uploaded copy (corrected ones)")
    args = parser.parse_args()
    {"tools-packages": tools_packages, "tools-notes": tools_notes, "tools-download": tools_download,
     "upload-manifests": upload_manifests}[args.command](args)


if __name__ == "__main__":
    main()
