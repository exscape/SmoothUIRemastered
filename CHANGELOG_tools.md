# v0.9.2
* Fixed: some mods giving errors (not just warnings) despite working well in-game. The merger is now more lenient, and able to merge/patch more mods.

# v0.9.1

* Added: support merging the HUD, making the entire HUD high framerate. Also tested together with a few of the top HUD mods (SAHUD, Friendly HUD).
* Fixed: allow mods with empty .bundle CRC values. Will allow some mods that previously failed to merge properly.
* Fixed: the merger could report "The game was updated" when in fact mods are what had changed.
* Build process reworked (use Nuitka instead of PyInstaller).

Note: HUD support requires Smooth UI v0.3.3 or later. Earlier builds will work with the merger, but the HUD will be ignored.

# v0.9.0

Initial release.  