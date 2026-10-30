# v0.9.3
* Improved: reduce warnings (mod X not merged, with a question to "Merge anyway") by making the merger smarter
* Improved: support rescaling mods that raise a screen's framerate only (e.g. Community GUI, HoodSlots), restoring the original animation speed but adding smoothness.
* Improved: support rescaling mods that ship unused game files (e.g. Community GUI's "Events", "Restored Content - Storybook", "Barber Anywhere")
* Improved: accept further mods with unusual formats (older .redswf versions, multiple DoABC tags)

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

* Added: support merging the HUD, making the entire HUD high framerate. Also tested together with a few of the top HUD mods (SAHUD, Friendly HUD).
* Fixed: allow mods with empty .bundle CRC values. Will allow some mods that previously failed to merge properly.
* Fixed: the merger could report "The game was updated" when in fact mods are what had changed.
* Build process reworked (use Nuitka instead of PyInstaller).

Note: HUD support requires Smooth UI v0.3.3 or later. Earlier builds will work with the merger, but the HUD will be ignored.

# v0.9.0

Initial release.  