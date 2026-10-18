// Smooth UI Remastered: Gwent speed options.
//
// The options are bin/config/r4game/user_config_matrix/pc/modSmoothUIRemastered.xml.
// When a match starts, this sends them to the code Smooth UI injects into gwint_game.redswf
// (smoothui.gwent.GwentSpeed), which registers the binding "smoothui.gwent.config" for them.
// Without that code (another mod's gwint_game, with no compatibility patch), they are simply ignored.
//
// Each option's value is extracted from its name in the XML (see SmoothUI_GwentOption), so options can be added or
// changed there alone (and in strings/en.csv, for their text).

// Before the settings menu shows the settings
@wrapMethod(CR4IngameMenu)
function showOptionsPanel() : void
{
	SmoothUI_InitOptions();
	wrappedMethod();
}

// The settings menu shows the description of the selected setting, which the game only gives two of its own
// settings: give Smooth UI's theirs, from the string option_<displayName>_description (if there is one).
// (Called for every setting of every group, as the menu lists them; optionName is a setting's id.)
// (A global function: @wrapMethod takes no class then.)
@wrapMethod
function SetDescriptionText(optionObject : CScriptedFlashObject, optionName : name, optionValue : string)
{
	var displayName : string;
	var description : string;

	wrappedMethod(optionObject, optionName, optionValue);
	displayName = theGame.GetInGameConfigWrapper().GetVarDisplayName('SmoothUIRemastered', optionName);
	if (StrBeginsWith(displayName, "smoothui_"))
	{
		description = GetLocStringByKeyExt("option_" + displayName + "_description");
		if (description != "")
		{
			optionObject.SetMemberFlashString("description", description);
		}
	}
}

@wrapMethod(CR4GwintGameMenu)
function SendCardValues() : void
{
	wrappedMethod();
	SmoothUI_SendGwentOptions(m_flashValueStorage);
}

// The version of the settings (in the XML) this script is for: raised when an update changes the options
// of a setting the user may have chosen already, so that SmoothUI_UpdateOptions moves the choice over.
// 1: up to 0.3.1 (nothing saved in SettingsVersion)
// 2: the effect settings go from Default/Off to Default/200%/300%/400%/Instant, and GwentLandingEffects is merged
//    into GwentCardEffects (which now decides for both)
function SmoothUI_SettingsVersion() : int
{
	return 2;
}

// A setting that was never set counts as its first option (to the game, and the menu), and the game has
// no defaults for a mod's settings (its own are in bin/config/base/*.ini). So the first time, this gives
// every setting its option named ..._default, and records that in the hidden setting DefaultsSet.
// Removes the need to ship a partial dx12user.settings which the user or mod manager merges.
// After that, it updates the user's choices to the current version of the settings (SmoothUI_UpdateOptions).
function SmoothUI_InitOptions()
{
	var config : CInGameConfigWrapper;
	var varName : name;
	var i : int;

	config = theGame.GetInGameConfigWrapper();
	if (config.GetVarValue('SmoothUIRemastered', 'DefaultsSet') == "true")
	{
		SmoothUI_UpdateOptions();
		return;
	}
	for (i = 0; i < config.GetVarsNumByGroupName('SmoothUIRemastered'); i += 1)
	{
		varName = config.GetVarNameByGroupName('SmoothUIRemastered', i);
		if (config.GetVarDisplayType('SmoothUIRemastered', varName) == "OPTIONS")
		{
			config.SetVarValue('SmoothUIRemastered', varName, IntToString(SmoothUI_DefaultOption(varName)));
		}
	}
	config.SetVarValue('SmoothUIRemastered', 'DefaultsSet', "true");
	// The defaults are for this version already
	config.SetVarValue('SmoothUIRemastered', 'SettingsVersion', IntToString(SmoothUI_SettingsVersion()));
	theGame.SaveUserSettings();
}

// The game saves a setting's choice as the index of the option, so when an update changes a setting's
// options, the user's choice would become whichever option is at that index now. This moves the choices
// made with an earlier version of the settings (in the hidden setting SettingsVersion) to the options
// they now are, one version at a time, and records the current version.
function SmoothUI_UpdateOptions()
{
	var config : CInGameConfigWrapper;
	var version : int;

	config = theGame.GetInGameConfigWrapper();
	version = SmoothUI_SavedSettingsVersion();
	if (version >= SmoothUI_SettingsVersion())
	{
		return;
	}
	// The version first: should it not be recorded, the choices are left alone, rather than moved again
	// next time (Off became Instant, but 1 is now 200%)
	config.SetVarValue('SmoothUIRemastered', 'SettingsVersion', IntToString(SmoothUI_SettingsVersion()));
	if (SmoothUI_SavedSettingsVersion() != SmoothUI_SettingsVersion())
	{
		LogChannel('modSmoothUIRemastered', "couldn't record SettingsVersion: settings left as they are");
		return;
	}
	if (version < 2)
	{
		// Default (0) stays the first option; Off (1) is now Instant. (The choice for landing effects, now card
		// effects too, is dropped.)
		SmoothUI_MoveOption('GwentCardEffects', 1, "_instant");
		SmoothUI_MoveOption('GwentWeatherIntros', 1, "_instant");
	}
	theGame.SaveUserSettings();
}

// The version of the settings saved in SettingsVersion (1 if there is none). (A slider: its value may be
// saved as a float.)
function SmoothUI_SavedSettingsVersion() : int
{
	return RoundF(StringToFloat(theGame.GetInGameConfigWrapper().GetVarValue('SmoothUIRemastered', 'SettingsVersion'), 1));
}

// If a setting is set to the option with index oldIndex (in an earlier version of the settings), sets it to
// the option whose name ends with suffix
function SmoothUI_MoveOption(varName : name, oldIndex : int, suffix : string)
{
	var config : CInGameConfigWrapper;
	var newIndex : int;

	config = theGame.GetInGameConfigWrapper();
	newIndex = SmoothUI_FindOption(varName, suffix);
	if (newIndex >= 0 && StringToInt(config.GetVarValue('SmoothUIRemastered', varName), 0) == oldIndex)
	{
		config.SetVarValue('SmoothUIRemastered', varName, IntToString(newIndex));
	}
}

// The index of a setting's option named ..._default (0, the first, if it has none)
function SmoothUI_DefaultOption(varName : name) : int
{
	return Max(SmoothUI_FindOption(varName, "_default"), 0);
}

// The index of a setting's option whose name ends with suffix (-1 if it has none)
function SmoothUI_FindOption(varName : name, suffix : string) : int
{
	var config : CInGameConfigWrapper;
	var i : int;

	config = theGame.GetInGameConfigWrapper();
	for (i = 0; i < config.GetVarOptionsNum('SmoothUIRemastered', varName); i += 1)
	{
		if (StrEndsWith(config.GetVarOption('SmoothUIRemastered', varName, i), suffix))
		{
			return i;
		}
	}
	return -1;
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
