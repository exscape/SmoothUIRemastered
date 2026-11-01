<!-- nexus:skip -->
# Smooth UI Tools

These tools are a companion to [Smooth UI Remastered](https://www.nexusmods.com/witcher3/mods/13526), which is required to use them. They are recommended for everyone: they make the HUD smooth, build Smooth UI for any frame rate from 60 to 240 fps, rebuild screens that game updates change, and make other UI mods work together with Smooth UI, by creating high frame rate versions of those mods.  
Download them from [their Nexus Mods page](https://www.nexusmods.com/witcher3/mods/15202).
<!-- /nexus:skip -->

## Quick start

1. Extract `Smooth UI Tools` from the .zip to any *writable* folder (not directly inside Program Files).  
   For most setups, `The Witcher 3\Smooth UI Tools` is a handy location. Mod Organizer 2 users must *not* place them inside the Witcher 3 directory, however: the merger refuses to run from there under MO2 (see the MO2 section below).
2. Start the game with `SmoothUILauncher.exe` instead of `witcher3.exe`. How depends on where you start the game:
   - **Steam:** set it in the game's launch options, and start the game from Steam (see "Setup: Steam" below).
   - **GOG Galaxy:** set it as the default executable, and start the game from Galaxy (see "Setup: GOG Galaxy").
   - **Otherwise:** a mod manager (see its section below) or a desktop shortcut to `SmoothUILauncher.exe`. (Create one in the merger app to get the Witcher icon automatically.)

   Yes, it's a launcher, but I've spent a lot of time ensuring that it's as fast as possible, and that it has none of the annoyances of the typical game launcher that everybody hates. <!-- nexus:skip --> It's invisible and unnoticeable (less than 1 second of extra launch time) unless there are issues that need your attention. <!-- /nexus:skip -->
3. The first time (and whenever you install, update, remove or reorder mods), the launcher opens the merger and tells you what needs fixing. <!-- nexus:skip -->Typically, this is a single button click that takes less than 10 seconds; the merger closes immediately when it's done, and the game launches.<!-- /nexus:skip -->

That's it.
The sections below cover Steam and GOG Galaxy first, then Mod Organizer 2. The other mod managers need nothing special, so their sections are optional tips.

Note: the downloaded folder contains many files due to the way they are built. Only `SmoothUILauncher.exe`, `SmoothUIMerger.exe` and the `data` folder (created on first start) are really relevant; everything else is static.

<!-- nexus:skip -->
**Setup:**

- [Steam](#setup-steam)
- [GOG Galaxy](#setup-gog-galaxy)
- [Mod Organizer 2](#setup-mod-organizer-2) (required steps)
- [Without a mod manager](#setup-without-a-mod-manager)
- [TW3MM and Relapse's Mod Manager](#setup-tw3mm-and-relapses-mod-manager)
- [Vortex](#setup-vortex)
<!-- /nexus:skip -->

Everything after the setup sections is optional reading: how the tools work, updates, limitations, troubleshooting and uninstallation.

## Setup: Steam

If you have the game on Steam, I recommend starting the launcher from Steam's launch options, with any mod manager except Mod Organizer 2. It's the cleaner way: the Steam overlay and related features (e.g. Steam game recording) keep working. Starting the launcher outside Steam may break Steam achievements (one user reported this; I can't confirm it).

**A Steam copy of the game:**
1. Find SmoothUILauncher.exe, right-click it, and choose "Copy as path".
2. Right-click the game in Steam > Properties > General. Paste the path under **Launch options**, and add ` %command%` afterwards. Example:
   ```
   "C:\Games\The Witcher 3\Smooth UI Tools\SmoothUILauncher.exe" %command%
   ```
3. Start the game from Steam.

**The game as a non-Steam game** (added with "Add a Non-Steam Game"):
1. Find SmoothUILauncher.exe, right-click it, and choose "Copy as path".
2. Right-click the game in Steam > Properties. Paste the path under **Target**, and you're done.

## Setup: GOG Galaxy

If you have the game on GOG Galaxy, I recommend setting up the launcher there, and starting the game from Galaxy:

1. Open the game in Galaxy, click the settings (sliders) icon, then **Manage installation > Configure > Features**.
2. Add another executable: `SmoothUILauncher.exe`, with no arguments, and set it as "Default executable", so that **Play** runs it.

## Setup: Mod Organizer 2

**Required:** [Evannee45's Witcher 3 plugin for MO2](https://www.nexusmods.com/witcher3/mods/9482). MO2 without it hasn't been tested.  
Due to MO2's design with the VFS, this setup is a bit more complex than for the other mod managers, but I assume MO2 users are somewhat used to the workflow.  
MO2 has to start the launcher itself, so the Steam and GOG Galaxy setups above don't apply. One user reported that starting the launcher outside Steam broke Steam achievements. It isn't confirmed, but if you have the Steam version, keep it in mind.

1. Keep Smooth UI Tools in a folder **outside** the Witcher 3 game folder. Inside it, MO2 would move the tools' own files (settings, logs, updates) into its overwrite folder, so the merger shows a message and exits instead of running there.
2. Make sure the plugin (above) is up-to-date: at least version 4.1 (released 2026-10-22). Older versions should work **if you have profile-specific game INI files enabled** for your current profile. (Check the Profiles window, *not* the Settings window.)
3. Right-click the mod list, **Create empty mod**, and name it, for example, "Smooth UI Merged Patches". Enable it.
4. In **Executables** (the gears icon), add `SmoothUILauncher.exe`. Tick **Create files in mod instead of overwrite** and select the mod from step 3.

Optionally, add `SmoothUIMerger.exe` the same way if you want to run it by hand (the launcher opens it for you when needed). **If added, it must use the same empty mod as the launcher.**

Always start the launcher and the merger **from MO2** or via shortcuts it creates: started any other way, they can't see MO2's mods, and will falsely claim e.g. that Smooth UI is not installed.

In the plugin's **Mod Load Order** panel (the Mods tab on the right), keep `mod0000_SmoothUI_MergedPatches` at the top. The plugin writes the game's load order from that panel, and if the patches aren't first there, the launcher keeps asking to merge.

## Setup: Without a mod manager

If you have the game on Steam or GOG Galaxy, start it from there instead: see the sections above (on Steam, this keeps the overlay working).

If this is not possible, start the game via a desktop or taskbar shortcut to `SmoothUILauncher.exe` instead of `witcher3.exe`. (The merger app can easily create one.)

If you install, remove or disable a mod (e.g. by renaming its folder to add `~` or `_` in front), the launcher will notice, and will ask to update the merged patches.

Alternatively, run `SmoothUIMerger.exe` manually any time you install/update/remove a mod that may change UI files, or when you change their load order.

## Setup: TW3MM and Relapse's Mod Manager

If you have the game on Steam or GOG Galaxy, start it from there instead: see the sections above (on Steam, this keeps the overlay working).

These are lightweight enough that the "Without a mod manager" section essentially applies.  
Both only allow for starting `witcher3.exe`, so you can't set the "Run the Game" button to use the launcher. The merger itself works with them: nothing special is needed, and the merged patches load first by default.

## Setup: Vortex

If you have the game on Steam or GOG Galaxy, start it from there instead: see the sections above (on Steam, this keeps the overlay working).

1. Click **Tools** (or the Dashboard's tool list), then the plus icon, and select `SmoothUILauncher.exe` as the target. The other fields can stay as they are.
2. On the new tool's tile, click the three dots and choose **Set as default launcher**. Vortex's Play button then runs the launcher for you.
3. Optional: add `SmoothUIMerger.exe` as a tool too, to run it by hand. You can always start it from outside Vortex, too.

If you start the game with desktop or taskbar shortcuts instead of Vortex, point them at `SmoothUILauncher.exe` instead of `witcher3.exe`.

## About the tools

Smooth UI Tools contains two programs:

### SmoothUIMerger.exe

Patches other mods' copies of the files Smooth UI changes, so that your other UI mods (for example: Sort Everything, Gwent mods, Separate Upgrades Tab, Community GUI fixes, and so on) keep working at Smooth UI's frame rate. It writes the result as one mod, `mod0000_SmoothUI_MergedPatches`, in your `Mods` folder.  
This is similar to how Script Merger works, but for making other UI mods work with Smooth UI.  
It also makes the game's HUD smooth, which Smooth UI can't do by itself, and builds every screen for the frame rate you choose (see "Frame rates" below).

### SmoothUILauncher.exe

Starts the game in place of `witcher3.exe`. Before the game starts, it checks (in well under a second) that the merged patches still match your mods. If they do, the game starts right away. If something changed (a mod was installed, updated, removed or reordered, or Smooth UI was updated), the merger opens first and tells you what needs fixing.

**If you start the game with the launcher, you never need to start the merger yourself:** the launcher opens it whenever your merged patches need updating.

The launcher is optional, but recommended: without it, nothing tells you when your merged patches are out of date, and you have to run the merger yourself after every change to your mods to ensure you get Smooth UI and other mods working together as intended.

## Frame rates

Smooth UI's downloads run at 60 or 120 fps. With the tools, you can use any rate from 60 to 240 fps: the merger builds every screen for it from the game's files. You set the rates on the first run's page, or under Settings.  
For new users, the default menu frame rate is set to your primary monitor's refresh rate. Change this if you play on a different monitor, or use a frame rate cap.

### Menus

This is the rate for the inventory, map, Gwent and every other screen except for the HUD. Enter:

- your frame rate cap, if you use one (also with VRR/G-Sync/FreeSync)
- otherwise, your display's refresh rate

If the rate you enter is higher than the game's actual frame rate, screens play slowly: a 144 fps build at 120 fps plays at 83% speed. Screens built for a lower rate (e.g. a 60 fps build played at 120 fps) play correctly, but not as smoothly.

### Smooth HUD

The "Smooth HUD" setting covers what's on screen during gameplay, for example: the list of picked-up items, floating damage numbers, the minimap and its quest arrow, the level-up animation, and the radial menu (choosing signs/weapons).  
I recommend using it unless it causes issues with your HUD mods. (If it does, please send a bug report!)

Only the merger can do this, so there's no Smooth UI HUD package to download. The HUD is made up of many files that only work together: if a compatibility patch were needed but missing, the HUD would break. The merger builds the whole HUD, or leaves it out if one or more files fail.

The HUD has two settings of its own, since gameplay usually runs slower than menus:

- **Frame rate in gameplay:** enter a typical low on an FPS counter, not the peak: at 70-90 fps, enter about 70. As with menus, too high plays slowly, while too low only looks a little less smooth.
- **Frame generation:** the multiplier, if you use DLSS or FSR frame generation. Otherwise, leave it off. Enter the frame rate you see, including generated frames: the HUD needs the rate before frame generation, so the tools divide by the multiplier for you.

### Mods that already run at 100+ fps

Screens from mods that already run at 100 fps or more (e.g. UI Remastered at 120 fps) are kept as they are, but Smooth UI's additions (e.g. Gwent speed options) are still applied. If your frame rate is lower than theirs, they play slowly, and the merger marks them "Plays slowly" (when they're below 85% speed). The tools don't cause this; they just warn you about it.  
To fix this, raise your frame rate cap and the Menus setting to the mod's rate, if your PC and display can reach it.

## Screens that only mods use

Some screens are in the game's files, but the game never shows them: cut content that mods bring back, such as the Glossary's Storybook (the "Events" tab of Community GUI fixes, or the "Restored Content - Storybook" mod).  
Smooth UI Remastered doesn't ship these, so it never gets in those mods' way, but if you have such a mod, the merger makes its screen smooth like any other, assuming it's a file known to it. If you use (or create) a mod that changes a screen that isn't handled, please open a bug report, and I'll look at supporting patching it.

## Updates

When the merger starts, it asks GitHub whether a newer version of Smooth UI Tools exists. If an update exists, the merger window says so: **What's new** shows the release notes, and **Update now** downloads and installs it.  
Updating is automatic: the merger downloads the new version, checks it, closes, and opens again in the new version.  
If the update fails, the old version stays as it was; you can then download the new version from [Nexus Mods](https://www.nexusmods.com/witcher3/mods/15202).

**Privacy:** the check is a standard web request to GitHub, which can see your IP address. The **only** other thing sent is the current tools version (as `SmoothUITools/X.Y.Z`). Nothing whatsoever is sent to me (the developer).  
**To turn it off:** untick "Check for updates when the merger starts" under **Settings** in the merger. The merger then never goes online at all.

## More information

- **Your mods are never changed:** The tools only write to their own `mod0000_SmoothUI_MergedPatches` mod, which they give priority in `mods.settings`. Everything else is left alone; deleting the patches reverts every change. (The `mods.settings` entry is harmless by itself, and mod managers may remove it automatically.)
- **What the merger fixes:** the game uses only one mod's copy of each UI file. When other mods change a file that Smooth UI also changes, the merger takes the copy the game would use anyway (from the mod that comes first in your load order) and adds Smooth UI's changes to it. So Smooth UI's own place in the load order doesn't matter.
- **What the merger doesn't do** is combine two **other** mods that change the same file: as without Smooth UI, only the first one's changes show, unless you install a compatibility patch for those two mods and place it above the original mods in the load order. The merger then uses that patch's copy and makes it high frame rate.
- **Load order:** the merged patches must load before every other UI mod (Priority 0 in `mods.settings`, which the merger sets). If another mod that ships one of the patched files is put ahead of them, the launcher notices and offers to fix it.
- **Script merger:** Script Merger may show conflicts involving `.redswf` files. **If `mod0000_SmoothUI_MergedPatches` is shown at the top of the conflict, you can ignore these warnings:** it doesn't know that we have resolved the issue already. In SM-FAE you can also go to Menu > Options... > Auto-Collapse in the Conflicts List and set "Not Mergeable conflicts" to hide all Smooth UI related "conflicts". Note that this will also hide other .redswf conflicts (conflicts about files Smooth UI does *not* change) if you have any.
- **Game updates:** if an update changes a screen, the merger tries to rebuild it from the new game files, without waiting for a new Smooth UI release.
- **mod.io mods are not supported** where they ship a UI file (`.redswf`) that Smooth UI changes: a mod.io mod always wins over regular mods, so neither Smooth UI nor the merger can fix that. mod.io mods that don't touch those files should be fine, but I haven't tested.
- **Mods that don't merge cleanly:** if the merger leaves out a mod's screen, or says some fixes couldn't be applied, please report it on the [Bugs tab](https://www.nexusmods.com/witcher3/mods/15202?tab=bugs) with the mod's name and version (the merger's "Write a bug report..." button collects the rest). Most such cases are gaps in Smooth UI Tools that can be fixed, but I need to know about them to fix them.
- **Logs:** if something goes wrong, use the "Write a bug report" button in the merger. You can also get a full .zip with all logs included, but Nexus bug reports can't have attachments. Upload it elsewhere and include a link, or send the report by creating a GitHub issue.
- Arguments given to the launcher are passed on to the game unchanged, which may be useful for e.g. Steam (see "Setup: Steam" above).

## Uninstallation

Aside from the merged patches, the tools only write to a folder named `data` under their install directory, so uninstalling them is simple: delete the directory you put them in.  
You should also delete `mod0000_SmoothUI_MergedPatches` from your mods directory.

<!-- nexus:skip -->
## Building the tools yourself (optional)

**You don't need this to use the tools:** the release zip is ready to use. This is only for those who want to build the exes from the source code themselves.

The releases are built by GitHub from the tagged source code, and you can check that a zip came from there: `gh attestation verify SmoothUITools-X.Y.Z.zip -R exscape/SmoothUIRemastered`. Your own build will work the same, but won't be bit-identical to the release.

On Windows, in `cmd.exe` or PowerShell:

1. Install [Python](https://www.python.org/downloads/) 3.14 (64-bit): the releases use 3.14.10. Python 3.15 doesn't work yet (Nuitka, which builds the exes, only partly supports it). Also install a C compiler: [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/), with "Desktop development with C++".
2. Get the source code of a release, and open `cmd.exe` in its folder. Either:
   - download "Source code (zip)" under the release on [GitHub](https://github.com/exscape/SmoothUIRemastered/releases), and extract it, or
   - with [Git](https://git-scm.com/):
     ```
     git clone https://github.com/exscape/SmoothUIRemastered.git
     cd SmoothUIRemastered
     git checkout tools-vX.Y.Z
     ```
3. Install the packages, at the exact versions the release used, in a new environment:
   ```
   py -3.14 -m venv env_gui
   env_gui\Scripts\python.exe -m pip install -r requirements_gui-lock.txt
   ```
4. Build (this takes a few minutes; the first build also downloads a tool Nuitka uses, Dependency Walker, into `%LOCALAPPDATA%\Nuitka`):
   ```
   env_gui\Scripts\python.exe build-tools.py
   ```
   The tools are then in `dist\SmoothUIMerger\`, ready to use; a new build there keeps their settings and logs (`data\`). Or add `--zip` to zip them as a release is zipped instead: `dist\SmoothUITools-X.Y.Z.zip`.
<!-- /nexus:skip -->
