
# v0.3.3

* Support making the HUD high FPS, but only when using [Smooth UI Tools](https://www.nexusmods.com/witcher3/mods/15202) because of how the HUD is built. A downloadable version would be *very* likely to cause compatibility issues, breaking the HUD when other mods change it.
  * The effects are quite small in general, but can be seen in the loot feed (the scrolling on the left side when you pick up several items), the radial wheel (signs/weapon selector), the quest arrow in the minimap, etc.

# v0.3.2

* Gwent: add card effects speed settings (from Default/Off to Default/200%/300%/400%/Instant) -- Instant is a better name than Off
* Gwent: add weather FX speed setting (only affects Clear Weather visually, but returns control to the player/AI faster for the others as well)
* Gwent: fix more base game bugs: remove red rectangle around the Hero card placement animation, and huge gray rectangle + red tint on Decoy
* Gwent: remove separate "landing effects" setting and use "card effects": the game separates them, but most players probably don't (Spy and Hero cards are "landing effects", everything else like Tight Bond, Muster, Scorch "card effects").
* Gwent: Improve a few animations, e.g. coin toss (glint returned), Tight Bond (smoother); a few others. Nothing major.

# v0.3.1

* Gwent speed options! The old sped-up versions were appreciated for Gwent especially, so this version adds proper controls to the Settings > Mods menu. There are five presets, and each parameter can be adjusted manually.
  * Personally I like the Insane preset: very fast, but keeps the things you may still want to see when experienced with the game.
* The .zip layout is changed: the settings requires a bin/ dir, so "modSmoothUIRemastered" is not at the root of the .zip now. Copying + merging all to the root TW3 install directory should work, and TW3MM + Vortex should handle it also.
* There are no other changes, so if you want the default Gwent speed, you can skip this version.
* All v0.3 compat patches still work with this version.

# v0.3

Big changes!

* New **120 fps everywhere** version, with reworked animations to improve smoothness
  * The 120 fps version is recommended for everyone with a 120+ Hz monitor that isn't using an FPS limiter set below 120. The FPS you get while playing is not relevant.
  * The 60 fps version is recommended for everyone else: e.g. 60/75/90/100 Hz monitors. At 100 Hz, the 120 fps version plays at 83.3% speed, with larger impact for lower refresh rates, so it works, but will likely feel slow.
  * Neither version is recommended if you use an FPS limiter set below 60 fps; use the default UI in that case.
* 60 fps version reworked to be 60 fps everywhere (v0.2 has 48 fps Gwent and transmog)
* **No animation speedup**, as in the older versions
* Base game bugs fixed:
  * UI: multiple tabs no longer show as selected/hovered simultaneously (e.g. the last three tabs on the blacksmith screen)
  * Gwent: fog/rain no longer draws outside of their rows, causing clouds to appear e.g. on your cards
  * Gwent: fog no longer causes flickers and other issues on other weather-affected rows
  * Gwent: fog intro animation no longer jumps partway through
  * Gwent: frost no longer flickers slightly
* The loading screen (loadingscreen.redswf) is no longer patched: it didn't make any difference in fluidity

This version targets and requires game version 5.01, and will not work on older patches.

# v0.2

* Target game version 5.01
  * Fixes a bug where you can't place waypoints on the map (mod v0.1 is not compatible with 5.01+)
* Remove plenty of files that don't need framerate patching: reduces mod size and improves compatibility (if you find a screen from the game that still needs patching, contact me!)

**There should be no visible changes from this; the files were unnecessary to patch.**

Full list of files that were patched in v0.1, but not in v0.2+, making Smooth UI Remastered v0.2+ automatically compatible with mods that patch only these .redswf files:

(All paths begin with gameplay/gui_new/swf/)  
character/panel_character_perks.redswf  
common/componentslib.redswf  
common/panel_common.redswf  
deathscreen/deathscreenmenu.redswf  
glossary/panel_glossary_storybook.redswf  
inventory/panel_inventorysockets.redswf  
loading/blackscreen.redswf  
loading/panel_loading_bob.redswf  
loading/panel_loading_islandofmyst.redswf  
loading/panel_loading_kaermorhen.redswf  
loading/panel_loading_novigrad.redswf  
loading/panel_loading_prologuevillage.redswf  
loading/panel_loading_prologuevillagewinter.redswf  
loading/panel_loading_skellige.redswf  
loading/panel_loading_spiral.redswf  
loading/panel_loading_velen.redswf  
loading/panel_loading_wyzima.redswf  
meditation/panel_meditation.redswf  
menubase.redswf  
photomode/photomode.redswf  
poster/panel_poster.redswf  
startscreen/panel_startscreen_ep1.redswf  
startscreen/panel_startscreen_ep2.redswf  
startscreen/panel_startscreen.redswf  
startup/panel_startup1.redswf  
startup/panel_startup_experience.redswf  
startup/panel_startup.redswf  

# v0.1

Initial release, targeting game version 5.01c.