// Smooth UI Remastered: the speed at which the stick scrolls text.
//
// The setting is in bin/config/r4game/user_config_matrix/pc/modSmoothUIRemastered.xml (the Other page;
// smoothui_settings.ws sets it up). The code Smooth UI injects into the menus with scrolling text
// (smoothui.text.StickScroll) asks for it on its first stick event, with the event below, having registered
// the binding "smoothui.text.config" for it. Without that code (another mod's copy of the menu), nothing asks.

@addMethod(CR4MenuBase)
event OnSmoothUIStickScrollConfig()
{
	SmoothUI_SendStickScrollOptions(m_flashValueStorage);
}

// Books are in a popup (panel_overlay)
@addMethod(CR4PopupBase)
event OnSmoothUIStickScrollConfig()
{
	SmoothUI_SendStickScrollOptions(m_flashValueStorage);
}

function SmoothUI_SendStickScrollOptions(storage : CScriptedFlashValueStorage)
{
	var options : CScriptedFlashObject;

	SmoothUI_InitOptions();
	options = storage.CreateTempFlashObject();
	// px per second at full tilt; StickScroll uses its own default for a value far off (0 if the setting is missing)
	options.SetMemberFlashNumber("fastSpeed", StringToFloat(theGame.GetInGameConfigWrapper().GetVarValue('SmoothUIRemasteredOther', 'StickScrollFastSpeed'), 0));
	storage.SetFlashObject("smoothui.text.config", options);
}
