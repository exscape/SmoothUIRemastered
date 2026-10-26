# Smooth UI Remastered for The Witcher 3

**If you only want to install the mod to play the game, [install it from the Nexus Mods page](https://www.nexusmods.com/witcher3/mods/13526) instead.**

**If you're looking for Smooth UI Tools (for players wanting compatibility with other UI mods), see [README_tools](README_tools.md).**

The rest of this file is about the developer tools that create the mod itself, and compatibility patches between Smooth UI and other mods, for mod authors to release.  
If you are a mod author that wants to release a compatibility patch, read on!

The script is tested under WSL (mainly) and in cmd.exe (Windows Terminal). Powershell also works (but venv activation is slightly different, use "activate.ps1").

**Please tell me if you run into any errors** that might mean the script is lacking functionality or has bugs -- for example, mods that cause the script to fail for whatever reason.

# Dependencies and installation

**Dependencies:**

* Python **[3.11 or newer](https://www.python.org/downloads/release/pymanager-263/)**. You can also use **Python install manager** from the Microsoft Store, but **avoid installing Python itself from the Microsoft Store**; use the install manager or a direct download.
* Some Python packages, see below
* WSL only: `unrar` (only required for patching .rar mods); see the `rarfile` package documentation (for Linux) if using WSL

WSL (Windows Subsystem for Linux) is supported, but entirely optional. I use it myself, which means it's more heavily tested, but please report any issues whether you use WSL, cmd.exe, or Powershell.

**Typical setup steps:**

| Step | Windows | WSL |
| --- | --- | --- |
| 1 |  `py -m venv env` | `python3 -m venv env` |
| 2 | `env/Scripts/activate.bat` | `. env/bin/activate (note the space!)` |
| 3 | `py -m pip install -r requirements.txt` | `pip install -r requirements.txt` |
| 4 | edit `config/config.toml` in any editor | Same |

**Step 4 is required before the first run.** The shipped `config/config.toml` holds the paths
from my own machine, so they most likely need to be changed to work for you.

# Usage

Smooth UI Remastered comes in two builds: 120 fps, and 60 fps: intended for displays (or frame limiters) below 120 Hz (including e.g. 75 Hz or 90 Hz). Their versions name the frame rate, e.g. `v0.3-120fps` and `v0.3-60fps`.

build-compat.py builds **two compatibility patches**, one for each build (`..._v0.3-120fps_..._Compat.zip` and `..._v0.3-60fps_..._Compat.zip`).  
Please release both:  
A 120 fps patch plays at 1/2 speed for 60 fps players (60 Hz monitor, or FPS limiter).  
A 60 fps patch works for everyone, but is less smooth, and may look off to 120 fps Smooth UI users, since only screens in your mod will be 60 fps.


Example:

    python build-compat.py --mod-name SortEverything --mod-version 1.53 --smooth-version 0.3 \
    '/mnt/e/Downloads/Sort Everything 1710 1.53 2026-10-08T18-32Z Jy2BxkOYp.zip'

This will:

* Unpack the mod to the work directory (work/compat/1_unpacked)
* Read the list of files in the mod's bundles
* For each build of Smooth UI Remastered v0.3 (120 and 60 fps):
  * Figure out the conflicting files between that build and the other mod
  * Create FPS-patched versions of the conflicting files, based on the other mod's .redswf files (work/compat/120fps/2_patched)
  * Bundle the FPS-patched files .redswf files *only* (work/compat/120fps/3_mod)
  * Zip the mod to a release-ready file next to the script
  * Write a manifest of the patch to `manifests/compat/` (not with `--only-fps` considered a dev/testing option), for checking it against later releases (see below)
* Delete work/compat entirely (use `--keep-work` to keep the files)

Optionally, you can add `--install --launch` to copy the mod to the game folder and launch the game for testing. This needs `--only-fps 120` or `--only-fps 60` to pick the patch matching the build of Smooth UI Remastered you have installed; `--only-fps` builds only that patch.  
Make sure that Smooth UI Remastered and the target mod, both of the correct version, are both already present in the mods folder! build-compat.py will only build the compatibility patch mod required to make the mods work together.

The conflicting files are patched with the same settings (and frame rate) as in the specified Smooth UI Remastered release (`--smooth-version`), which should work for most mods.
If a mod changes identifiers in an incompatible way, or needs more control, `--overrides FILE` adds settings on top; see `config/compat_overrides_example.txt` for the format.

**Does a new Smooth UI Remastered release need a new compat patch?**  
Not always. After updating this repository (to download the new release's manifests), run `python version-tool.py --check-compat-patch YourModName`.  
This only reads the manifests: for your mod's patches (those of its latest version), it tells whether they still work with that release as they are, or need a rebuild because the release patches their files differently (or now patches more of your mod's files).  
This needs the manifests that build-compat.py wrote in your copy's `manifests/compat/`, so keep those around. It doesn't know about new versions of your mod; for those, see below.

**Does a new version of your mod need a new compat patch?**
Only if it changed a .redswf file that Smooth UI Remastered patches. Just build the patch: build-compat.py compares it with the newest earlier patch for your mod (from its manifest in `manifests/compat/`), and warns if the new one is identical, meaning neither your files nor the patching changed. The earlier patch then works with the new version as it is.

Run `python3 build-compat.py --help` for basic help.
Feel free to [contact me on Nexus Mods](https://www.nexusmods.com/profile/aeluwas?gameId=952), or create a GitHub issue if something is wrong or unclear.

# Project structure:

| File | Description |
| --- | --- |
| `build-compat.py` | Builds a compatibility patch against another mod (zip/rar/7z) |
| `build-main.py` | Builds the mod; mainly intended for my own use |
| `merger-gui.py` | Smooth UI Tools GUI, see `README_tools.md` |
| `launcher.py` | Smooth UI Tools launcher, see `README_tools.md` | 
| `merge.py` | Development tool; implements some of Smooth UI Tools in a command-line form |
| `version-tool.py` | Reads the manifests (`manifests/`): checks whether compat patches are up-to-date, and can also `--diff` two release manifests to show changes, or `--check-game-files` to see whether a game update changed files the mod patches. |
| `assets/` | Various files: icons, strings, mod settings XML and so on. | 
| `smoothui/common.py` | Various shared logic |
| `smoothui/build/` | The build process: implements `build-main.py` (`main.py`), `build-compat.py` (`compat.py`) and `version-tool.py`, and reads profiles, override files and reviewed warnings (see `config/`) |
| `smoothui/merger/` | Smooth UI Merger implementation (see `README_tools.md`) |
| `smoothui/patcher/` | Binary patcher; creates new, higher framerate `.redswf` files from a given list of files |
| `smoothui/patcher/swf_patcher/` | Rescales a single SWF: retimes its animations for the new frame rate, and adjusts the ActionScript code that depends on frame numbers |
| `smoothui/patcher/fixes/` | Pre- or post-FPS patch edits. Fixes for base game bugs, and non-FPS-related Smooth UI features are added here. |
| `smoothui/witcher3/` | Support library for Witcher 3-specific files (.redswf, .bundle, metadata.store) |