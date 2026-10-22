# Smooth UI Tools

These tools are a companion to [Smooth UI Remastered](https://www.nexusmods.com/witcher3/mods/13526), which is required to use them. They make other UI mods work together with Smooth UI, by creating high framerate versions of those mods.  
You only need these tools if you use other UI mods that conflict with Smooth UI.

**This file might be overwhelming, but there's really not a ton you need to do. If you just want to get started, jump to [Installation](#installation).**

Smooth UI Tools contains two programs:

### SmoothUIMerger.exe

Patches other mods' copies of the files Smooth UI changes, so that your other UI mods (for example: Sort Everything, Gwent mods, Separate Upgrades Tab, Community GUI fixes, and so on) keep working at Smooth UI's frame rate. It writes the result as one mod, `mod0000_SmoothUI_MergedPatches`, in your `Mods` folder.  
This is similar to how Script Merger works, but for making other UI mods work with Smooth UI.

### SmoothUILauncher.exe

Starts the game in place of `witcher3.exe`. Before the game starts, it checks (in well under a second) that the merged patches still match your mods. If they do, the game starts right away. If something changed (a mod was installed, updated, removed or reordered, or Smooth UI was updated), the merger opens first and tells you what needs fixing.  
Typically, this is a single button click that takes less than 5 seconds; the merger closes immediately when it's done, and the game launches.

**If you start the game with the launcher, you never need to start the merger yourself:** the launcher opens it whenever your merged patches need updating. You can still open it to see the details. 

The launcher is optional, but recommended: without it, nothing tells you when your merged patches are out of date, and you have to run the merger yourself after every change to your mods to ensure you get Smooth UI and other mods working together as intended.  
I've spent a lot of time ensuring that it's as fast as possible, and that it has none of the annoyances of the typical game launcher that everybody hates.

## Installation

Extract all the files and folders in the .zip to any *writable* folder (not directly inside Program Files).  
Mod Organizer 2 users should *not* place them inside the Witcher 3 directory as that is likely to cause issues. For other mod managers (or none at all), `The Witcher 3\Smooth UI Tools` is a handy location.

Also see the setup section below on how to connect the tools to your mod manager.

**Uninstallation**
  
Aside from the merged patches, the tools only write to a folder named `data` under its install directory, so uninstalling them is simple: delete the directory you put them in.  
You should also delete `mod0000_SmoothUI_MergedPatches` from your mods directory.

**Setup:**

- [Without a mod manager](#setup-without-a-mod-manager)
- [TW3MM and Relapse's Mod Manager](#setup-tw3mm-and-relapses-mod-manager)
- [Vortex](#setup-vortex)
- [Mod Organizer 2](#setup-mod-organizer-2)
- [Optional: launching via Steam or GOG Galaxy](#launching-via-steam-or-gog-galaxy)

## Good to know

- **What the merger fixes:** the game uses only one mod's copy of each UI file. When other mods change a file that Smooth UI also changes, the merger takes the copy the game would use anyway (from the mod that comes first in your load order) and adds Smooth UI's changes to it. So Smooth UI's own place in the load order doesn't matter.
- **What the merger doesn't do** is combine two **other** mods that change the same file: as without Smooth UI, only the first one's changes show, unless you install a compatibility patch for those two mods and place it above the original mods in the load order. The merger then uses that patch's copy and makes it high framerate.
- **Load order:** the merged patches must load before every other UI mod (Priority 0 in `mods.settings`, which the merger sets). If another mod that ships one of the patched files is put ahead of them, the launcher notices and offers to fix it.
- **Game updates** may need a new version of Smooth UI.
- **mod.io mods are not supported** where they ship a UI file (`.redswf`) that Smooth UI changes: a mod.io mod always wins over regular mods, so neither Smooth UI nor the merger can fix that. mod.io mods that don't touch those files should be fine, but I haven't tested.
- **Removing Smooth UI:** if you disable or uninstall Smooth UI but leave the merged patches, the launcher tells you, and offers to remove them.
- **Logs:** if something goes wrong, `data\launcher.log` and `data\merger.log` (next to the exes) say what happened. Please include them when you report a problem.
- Arguments given to the launcher are passed on to the game unchanged, which may be useful for e.g. Steam (see Steam below).

## Setup: Without a mod manager

Start the game with `SmoothUILauncher.exe` instead of `witcher3.exe`, for example from a desktop shortcut. If you install, remove or disable a mod (e.g. by renaming its folder to add `~` or `_` in front), the launcher will notice, and will ask to update the merged patches.

Alternatively, run `SmoothUIMerger.exe` manually any time you install/update/remove a mod that may change UI files, or when you change their load order.

## Setup: TW3MM and Relapse's Mod Manager

The Witcher 3 Mod Manager (TW3MM) and Relapse's Mod Manager only start `witcher3.exe`, so you can't use the launcher for their "Run the Game" button. The merger itself works with them: nothing special is needed, and the merged patches load first by default.

Either start the game with the launcher outside the manager (for example from a desktop shortcut to the launcher), or run `SmoothUIMerger.exe` yourself after you install/update/remove or reorder mods that may change UI files.

## Setup: Vortex

1. Click **Tools** (or the Dashboard's tool list), then the plus icon, and select `SmoothUILauncher.exe` as the target. The other fields can stay as they are.
2. On the new tool's tile, click the three dots and choose **Set as default launcher**. Vortex's Play button then runs the launcher for you.
3. Optional: add `SmoothUIMerger.exe` as a tool too, to run it by hand. You can always start it from outside Vortex, too.

If you start the game with desktop or taskbar shortcuts instead of Vortex, point them at `SmoothUILauncher.exe` instead of `witcher3.exe`.

## Setup: Mod Organizer 2

**Required:** [Evannee45's Witcher 3 plugin for MO2](https://www.nexusmods.com/witcher3/mods/9482). MO2 without it hasn't been tested.
Due to MO2's design with the VFS, this setup is a bit more complex than for the other mod managers, but I assume MO2 users are somewhat used to the workflow.

1. Make sure the plugin (above) is up-to-date: at least version 4.1 (released 2026-10-22). Older versions should work **if you have profile-specific game INI files enabled** for your current profile. (Check the Profiles window, *not* the Settings window.)
2. Right-click the mod list, **Create empty mod**, and name it, for example, "Smooth UI Merged Patches". Enable it.
3. In **Executables** (the gears icon), add `SmoothUILauncher.exe` and `SmoothUIMerger.exe`. For **both**, tick **Create files in mod instead of overwrite** and select the mod from step 2. **They must use the same empty mod.**

Always start the launcher and the merger **from MO2** or via shortcuts it creates: started any other way, they can't see MO2's mods, and will falsely claim e.g. that Smooth UI is not installed.

In the plugin's **Mod Load Order** panel (the Mods tab on the right), keep `mod0000_SmoothUI_MergedPatches` at the top. The plugin writes the game's load order from that panel, and if the patches aren't first there, the launcher keeps asking to merge.

## Optional: launching via Steam or GOG Galaxy

To make the "Play" button in Steam or GOG Galaxy use the launcher:

### Steam

- **The game as a non-Steam game** (added with "Add a Non-Steam Game"): set the shortcut's **Target** to `SmoothUILauncher.exe`. Tested: Steam shows the game as running, and the overlay works. Launch options with `%command%` don't work for a non-Steam game: the game starts without the launcher.
- **A Steam copy of the game: not tested yet.** I believe that it should work with these launch options (right-click the game > Properties > General), with the path to your copy of the launcher:
  ```
  "C:\path\to\SmoothUILauncher.exe" %command%
  ```
  If you try this, please report whether the launcher runs (`data\launcher.log` is written, and its first line shows what Steam passed), whether Steam shows the game as running, and whether the overlay and playtime work.

### GOG Galaxy

1. Open the game in Galaxy, click the settings (sliders) icon, then **Manage installation > Configure > Features**.
2. Add another executable: `SmoothUILauncher.exe`, with no arguments, and set it as "Default executable", so that **Play** runs it.

With no arguments, the launcher starts the DirectX 12 game (`bin\x64_dx12\witcher3.exe`). For DirectX 11, give it the full path of `bin\x64\witcher3.exe` as its argument (not tested yet).