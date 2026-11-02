# v0.9.5
**Note:** The old versions have a bug during updates (about 10% of the time).  
If the update fails: close the errors (likely two of them; the game starts anyway), then start SmoothUIMerger.exe directly to update again. You can also re-download from Nexus Mods, just this once.

* **Critical fix:** Fix update check for launcher-only users, who never saw any update notices before
* Fixed: occasional crash on update from the launcher, see above
* Added: allow disabling merging for screens that merged cleanly, just in case / for troubleshooting; show mod's default frame rate
* Improved: warn in Settings when game settings (FPS limiter, frame generation settings) would cause slow-motion menus or HUD

This is marked as a critical update, as that is the only way I can reach some users: all older versions hide all non-critical updates when started by the launcher, which means most users still use the version they first downloaded.

# v0.9.4
* Changed: "Merge the HUD too" is now called "Smooth HUD".
* Added: Any FPS: 60 to 240 fps, no longer only Smooth UI's 60 and 120 fps downloads. The merger builds every screen for it from the game's files (Settings > Frame rate). Mod screens that already run at 100+ fps are kept as they are.
* Added: Smooth HUD gets its own frame rate, since gameplay usually runs slower than menus. Frame generation is taken into account.
* Added: after a game update, the merger tries to rebuild changed screens from the new game files, without waiting for a new Smooth UI release.
* Improved: support for more mods, e.g. Map Remastered (Map and Icons Remastered), with and without Smooth Map and World Map Merger.
* Improved: more lenient patcher: fixes "unexpected bytes after the End tag" issue that happened with some mods (e.g. Better Vanilla UI v0.1).
* Docs: on Steam and GOG Galaxy, I now recommend starting the launcher from there. On Steam, this keeps the overlay working.

Existing setups don't change until you change your frame rate settings. If your gameplay runs below your Smooth UI download's frame rate, set Smooth HUD's frame rate in Settings, or the HUD plays slowly. For more information on how to choose them, see "Frame rates" on the Nexus page or in README_tools.md.

# v0.9.3
* Improved: fewer "not merged" warnings: the merger is smarter, and doesn't need to ask as often.
* Improved: support rescaling mods that raise a screen's frame rate only (e.g. Community GUI, HoodSlots), restoring the original animation speed but adding smoothness.
* Improved: support rescaling mods that ship unused game files (e.g. Community GUI's "Events", "Restored Content - Storybook", "Barber Anywhere").
* Improved: accept further mods with unusual formats (older .redswf versions, multiple DoABC tags).

These are fairly large changes behind the scenes.

**Resolves issues with:**
* Spectre (Remastered Edition): previously failed entirely
* Reforge and Transmog Overhaul: previously failed entirely
* Community GUI: mod's animation speedup resolved, now works at 120 fps
* Restored Content - Storybook: mod's animation speedup resolved, now works at 120 fps
* Barber Anywhere: now works at 120 fps

...and most likely many others I haven't tested.

# v0.9.2
* Added: support for me (the dev) marking screens/mods as "known not to work with Smooth UI", to avoid every user getting warnings about them, every time.
* Added: support for the user saying "don't merge, and don't ask again" for a mod's screen, to handle cases similar to the above not yet handled by me; the screen is then shown as "Turned off" instead of as a warning.
* Improved: clean up text during warnings/failures: the very technical information is now in the log, and a more reasonable explanation shown in the merger GUI. The log is also cleaned up.
* Improved: HUD merge failures shouldn't spam the logs as much (one warning for all files left out, instead of one per HUD file).
* Fixed: some mods giving errors (not just warnings) despite working well in-game. The merger is now more lenient, and able to merge/patch more mods.
* Fixed: Smooth UI "fixes" (including Gwent speed options) can be applied to more files, as some were incorrectly excluded before.
* Fixed: multiple HUD mods could cause GUI texts to be cut off unless you manually resized the window.

**Mods that should now work:**
* HoodSlots
* Expanded Buff Menu
* CMF Minimap Panel (part of Conjunction Map Framework -- the map screen is 60 fps and cannot be safely patched)
* UI Remastered Edition (except the HUD): only Gwent is relevant, which now gets the Smooth UI speed settings

# v0.9.1

* Added: support merging the HUD, making the entire HUD high frame rate. Also tested together with a few of the top HUD mods (SAHUD, Friendly HUD).
* Fixed: allow mods with empty .bundle CRC values. Will allow some mods that previously failed to merge properly.
* Fixed: the merger could report "The game was updated" when in fact mods are what had changed.
* Build process reworked (use Nuitka instead of PyInstaller).

Note: HUD support requires Smooth UI v0.3.3 or later. Earlier builds will work with the merger, but the HUD will be ignored.

# v0.9.0

Initial release.  