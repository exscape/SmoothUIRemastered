# Smooth UI Remastered for The Witcher 3

**If you only want to install the mod to play the game, [install it from the Nexus Mods page](https://www.nexusmods.com/witcher3/mods/13526) instead.**

These are the tools that creates the mod itself. They can also create compatibility patches between this mod and other mods that modify the same .redswf files.  
If you are a mod author that wants to release a compatibility patch, read on!

The script is tested under WSL (mainly) and in cmd.exe (Windows Terminal). Powershell probably works as well (but venv activation is slightly different).

**Please tell me if you run into any errors** that might mean the script is lacking functionality or has bugs -- for example, mods that cause the script to fail for whatever reason.

# Dependencies and installation

**Dependencies:**

* Python **3.9 or newer (developed/tested on 3.11)**
* Witcher 3 REDkit (specifically bin\x64_RedKit\wcc_lite.exe)
* Some Python packages, see below
* WSL only: `unrar` (only required for patching .rar mods); see the `rarfile` package documentation (for Linux) if using WSL

WSL (Windows Subsystem for Linux) is supported, but entirely optional. I use it myself, which means it's more heavily tested, but please report any issues whether you use WSL, cmd.exe, or Powershell.

**Typical setup steps:**

| Step | Windows | WSL |
| --- | --- | --- |
| 1 |  `py -m venv env` | `python3 -m venv env` |
| 2 | `env/Scripts/activate.bat` | `. env/bin/activate (note the space!)` |
| 3 | `pip install -r requirements.txt` | Same |
| 4 | edit `config/config.toml` in any editor | Same |

**Step 4 is required before the first run.** The shipped `config/config.toml` holds the paths
from my own machine, so they most likely need to be changed to work for you.

# Usage

Example:

    python3 build-compat.py --mod-name SkillSlotPages --mod-version 1.7a --smooth-version 0.2 \
    '/mnt/e/Downloads/ModSkillSlotPages Remaster 1.7a [...].7z'

This will:

* Unpack the mod to the work directory (work/compat/1_unpacked)
* Unbundle the mod (work/compat/2_unbundled)
* Figure out the conflicting files between Smooth UI Remastered (v0.2, as specified) and the other mod
* Create FPS-patched versions of the conflicting files, based on the other mod's .redswf files (work/compat/3_patched)
* Bundle the FPS-patched files .redswf files *only* (work/compat/4_mod)
* Zip the mod to a release-ready file next to the script (which is the only time the --mod-name and --mod-version arguments are used)
* Delete work/compat entirely (use `--keep-work` to keep the files)

Optionally, you can add `--install --launch` to copy the mod to the game folder and launch the game for testing.  
Make sure that Smooth UI Remastered and the target mod, both of the correct version, are both already present in the mods folder! build-compat.py will only build the compatibility patch mod required to make the mods work together.

Run `python3 build-compat.py` for basic help. I'm considering making a simple (and optional) GUI to streamline things further, and a binary release (likely PyInstaller-based) to go with it.

# Project structure:

| File | Description |
| --- | --- |
| `build-compat.py` | Builds a compatibility patch against another mod (zip/rar/7z) |
| `build-main.py` | Builds the mod; mainly intended for my own use |
| `smoothui/build_compat.py` | Actual `build-compat.py` implementation |
| `smoothui/build_main.py` | Actual `build-main.py` implementation |
| `smoothui/build_common.py` | Shared implementation details for the `build-*` tools |
| `smoothui/common.py` | Various shared logic |
| `smoothui/patcher.py` | Binary patcher; creates new, higher framerate `.redswf` files from a given list of files |

Only build-main.py and build-compat.py can be called as scripts.
