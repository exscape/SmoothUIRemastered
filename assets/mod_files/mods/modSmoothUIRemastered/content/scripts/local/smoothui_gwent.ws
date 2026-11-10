// Smooth UI Remastered: Gwent speed options.
//
// The options are bin/config/r4game/user_config_matrix/pc/modSmoothUIRemastered.xml
// (smoothui_settings.ws sets them up).
// When a match starts, this sends them to the code Smooth UI injects into gwint_game.redswf
// (smoothui.gwent.GwentSpeed), which registers the binding "smoothui.gwent.config" for them.
// Without that code (another mod's gwint_game, with no compatibility patch), they are simply ignored.
//
// Each option's value is extracted from its name in the XML (see SmoothUI_GwentOption), so options can be added or
// changed there alone (and in assets/strings/en.csv, for their text).

@wrapMethod(CR4GwintGameMenu)
function SendCardValues() : void
{
	wrappedMethod();
	SmoothUI_SendGwentOptions(m_flashValueStorage);
}

function SmoothUI_SendGwentOptions(storage : CScriptedFlashValueStorage)
{
	var options : CScriptedFlashObject;

	SmoothUI_InitOptions();
	options = storage.CreateTempFlashObject();
	// Percent of the game's card speed (2000 px per second); 0: instant
	options.SetMemberFlashNumber("cardSpeed", SmoothUI_GwentOption('GwentCardSpeed', "smoothui_gwent_card_speed_", 100));

	// Seconds; -1: the game's own (1.2 s for the opponent's card, 2.3 s for banners); 0: not shown at all
	options.SetMemberFlashNumber("opponentCardDelay", SmoothUI_GwentOption('GwentOpponentCardTime', "smoothui_gwent_opponent_card_time_", -1));
	options.SetMemberFlashNumber("turnBannerHold", SmoothUI_GwentOption('GwentTurnBannerHold', "smoothui_gwent_banner_hold_", -1));
	options.SetMemberFlashNumber("firstBannerHold", SmoothUI_GwentOption('GwentFirstBannerHold', "smoothui_gwent_banner_hold_", -1));
	options.SetMemberFlashNumber("resultBannerHold", SmoothUI_GwentOption('GwentResultBannerHold', "smoothui_gwent_banner_hold_", -1));
	options.SetMemberFlashNumber("otherBannerHold", SmoothUI_GwentOption('GwentOtherBannerHold', "smoothui_gwent_banner_hold_", -1));
	// Card effect speeds: multiples of the game's speed (1: as in the game); 0: skipped
	options.SetMemberFlashNumber("cardEffects", SmoothUI_GwentOption('GwentCardEffects', "smoothui_gwent_effect_", 1));
	options.SetMemberFlashNumber("weatherIntros", SmoothUI_GwentOption('GwentWeatherIntros', "smoothui_gwent_effect_", 1));
	storage.SetFlashObject("smoothui.gwent.config", options);
}

// An option's value, from the name (displayName) of the chosen option in the XML: prefix, then the value
// with _ for the decimal point (smoothui_gwent_banner_hold_0_75: 0.75), "default" for defaultValue, or
// "instant" for 0 (no time: for card speed).
// For an OPTIONS setting, the game stores the index of the chosen option.
function SmoothUI_GwentOption(varName : name, prefix : string, defaultValue : float) : float
{
	var config : CInGameConfigWrapper;
	var option : string;

	config = theGame.GetInGameConfigWrapper();
	option = config.GetVarOption('SmoothUIRemastered', varName, StringToInt(config.GetVarValue('SmoothUIRemastered', varName), 0));
	if (!StrBeginsWith(option, prefix))
	{
		return defaultValue;
	}
	option = StrAfterFirst(option, prefix);
	if (option == "" || option == "default")
	{
		return defaultValue;
	}
	if (option == "instant")
	{
		return 0;
	}
	return StringToFloat(StrReplaceAll(option, "_", "."), defaultValue);
}
