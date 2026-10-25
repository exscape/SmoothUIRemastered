#!/usr/bin/env python3

# pyinstaller-hashes.py -- print the SHA-256 of a PyInstaller version's prebuilt 64-bit Windows bootloaders,
# as an entry for PREBUILT_BOOTLOADERS in smoothui-tools.spec (which refuses to build with those). Needed
# for each new PyInstaller version. They come from its official win_amd64 wheel on PyPI; its sdist holds
# the same files.
#
# Usage: python dev-tools/pyinstaller-hashes.py VERSION        e.g. 6.22.3

import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile

BOOTLOADER_DIR = "PyInstaller/bootloader/Windows-64bit-intel/"


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python dev-tools/pyinstaller-hashes.py VERSION")
    version = sys.argv[1]
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/pyinstaller/{version}/json") as response:
            files = json.load(response)["urls"]
    except urllib.error.HTTPError as e:
        sys.exit(f"PyInstaller {version} not found on PyPI ({e})")
    wheel = next((f for f in files if f["filename"].endswith("-win_amd64.whl")), None)
    if wheel is None:
        sys.exit(f"PyInstaller {version} has no win_amd64 wheel on PyPI")
    with urllib.request.urlopen(wheel["url"]) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != wheel["digests"]["sha256"]:
        sys.exit(f"{wheel['filename']}: the download doesn't match PyPI's SHA-256")

    with zipfile.ZipFile(io.BytesIO(data)) as wheel_zip:
        names = sorted(n for n in wheel_zip.namelist() if n.startswith(BOOTLOADER_DIR) and n.endswith(".exe"))
        print(f'    "{version}": {{')
        for name in names:
            print(f'        "{name[len(BOOTLOADER_DIR):]}": "{hashlib.sha256(wheel_zip.read(name)).hexdigest()}",')
        print("    },")


if __name__ == "__main__":
    main()
