// Smooth UI Remastered: its settings menu (bin/config/r4game/user_config_matrix/pc/modSmoothUIRemastered.xml).
//
// Gives every setting its default the first time, moves the user's choices over when an update changes the
// settings, and shows the settings' descriptions in the menu. The scripts that use the settings read them
// (smoothui_gwent.ws: the Gwent settings).

// Before the settings menu shows the settings
@wrapMethod(CR4IngameMenu)
function showOptionsPanel() : void
{
	SmoothUI_InitOptions();
	wrappedMethod();
}

// The settings menu shows the description of the selected setting, which the game only gives two of its own
// settings: give Smooth UI's theirs, from the string option_<displayName>_description (if there is one).
// (Called for every setting of every group, as the menu lists them; optionName is a setting's id, which is
// unique among Smooth UI's groups.)
// (A global function: @wrapMethod takes no class then.)
@wrapMethod
function SetDescriptionText(optionObject : CScriptedFlashObject, optionName : name, optionValue : string)
{
	var groups : array<name>;
	var displayName : string;
	var description : string;
	var i : int;

	wrappedMethod(optionObject, optionName, optionValue);
	groups = SmoothUI_Groups();
	for (i = 0; i < groups.Size(); i += 1)
	{
		displayName = theGame.GetInGameConfigWrapper().GetVarDisplayName(groups[i], optionName);
		if (StrBeginsWith(displayName, "smoothui_"))
		{
			description = GetLocStringByKeyExt("option_" + displayName + "_description");
			if (description != "")
			{
				optionObject.SetMemberFlashString("description", description);
			}
			break;
		}
	}
}

// The version of the settings (in the XML, all groups) this script is for: raised when an update changes the
// options of a setting the user may have chosen already, so that SmoothUI_UpdateOptions moves the choice over.
// Kept in the hidden setting SettingsVersion of the group SmoothUIRemastered.
// 1: up to 0.3.1 (nothing saved in SettingsVersion)
// 2: the effect settings go from Default/Off to Default/200%/300%/400%/Instant, and GwentLandingEffects is merged
//    into GwentCardEffects (which now decides for both)
function SmoothUI_SettingsVersion() : int
{
	return 2;
}

// Smooth UI's groups of settings (each a page in the menu): those whose id starts with SmoothUIRemastered
function SmoothUI_Groups() : array<name>
{
	var config : CInGameConfigWrapper;
	var groups : array<name>;
	var group : name;
	var i : int;

	config = theGame.GetInGameConfigWrapper();
	for (i = 0; i < config.GetGroupsNum(); i += 1)
	{
		group = config.GetGroupName(i);
		if (StrBeginsWith(NameToString(group), "SmoothUIRemastered"))
		{
			groups.PushBack(group);
		}
	}
	return groups;
}

// A setting that was never set counts as its first option (to the game, and the menu), or 0 for a slider,
// and the game has no defaults for a mod's settings (its own are in bin/config/base/*.ini). So the first
// time, this gives every setting of a group its default (SmoothUI_SetDefaults), and records that in the
// group's hidden setting DefaultsSet. Removes the need to ship a partial dx12user.settings which the user or
// mod manager merges. It also updates the user's choices to the current version of the settings
// (SmoothUI_UpdateOptions); first, so that a group which only gets its defaults now has them for the
// current version, whatever the updates did to it.
function SmoothUI_InitOptions()
{
	var config : CInGameConfigWrapper;
	var groups : array<name>;
	var saveNeeded : bool;
	var i : int;

	SmoothUI_UpdateOptions();
	config = theGame.GetInGameConfigWrapper();
	groups = SmoothUI_Groups();
	for (i = 0; i < groups.Size(); i += 1)
	{
		if (config.GetVarValue(groups[i], 'DefaultsSet') != "true")
		{
			SmoothUI_SetDefaults(groups[i]);
			saveNeeded = true;
		}
	}
	if (saveNeeded)
	{
		theGame.SaveUserSettings();
	}
}

// Gives every setting of a group its default: an OPTIONS setting its option named ..._default, a slider the
// value from SmoothUI_SliderDefault. Then sets the group's DefaultsSet.
function SmoothUI_SetDefaults(group : name)
{
	var config : CInGameConfigWrapper;
	var varName : name;
	var displayType : string;
	var value : string;
	var i : int;

	config = theGame.GetInGameConfigWrapper();
	for (i = 0; i < config.GetVarsNumByGroupName(group); i += 1)
	{
		varName = config.GetVarNameByGroupName(group, i);
		displayType = config.GetVarDisplayType(group, varName);
		if (displayType == "OPTIONS")
		{
			config.SetVarValue(group, varName, IntToString(SmoothUI_DefaultOption(group, varName)));
		}
		else if (StrBeginsWith(displayType, "SLIDER"))
		{
			value = SmoothUI_SliderDefault(varName);
			if (value != "")
			{
				config.SetVarValue(group, varName, value);
			}
		}
	}
	config.SetVarValue(group, 'DefaultsSet', "true");
}

// A slider's default value (as the settings store it: the value itself). Every slider needs one here, which
// the build checks: there's no way to give one in the XML.
function SmoothUI_SliderDefault(varName : name) : string
{
	switch (varName)
	{
		// The defaults are for this version already
		case 'SettingsVersion':
			return IntToString(SmoothUI_SettingsVersion());
	}
	LogChannel('modSmoothUIRemastered', "no default for the slider " + NameToString(varName));
	return "";
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
		SmoothUI_MoveOption('SmoothUIRemastered', 'GwentCardEffects', 1, "_instant");
		SmoothUI_MoveOption('SmoothUIRemastered', 'GwentWeatherIntros', 1, "_instant");
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
function SmoothUI_MoveOption(group : name, varName : name, oldIndex : int, suffix : string)
{
	var config : CInGameConfigWrapper;
	var newIndex : int;

	config = theGame.GetInGameConfigWrapper();
	newIndex = SmoothUI_FindOption(group, varName, suffix);
	if (newIndex >= 0 && StringToInt(config.GetVarValue(group, varName), 0) == oldIndex)
	{
		config.SetVarValue(group, varName, IntToString(newIndex));
	}
}

// The index of a setting's option named ..._default (0, the first, if it has none)
function SmoothUI_DefaultOption(group : name, varName : name) : int
{
	return Max(SmoothUI_FindOption(group, varName, "_default"), 0);
}

// The index of a setting's option whose name ends with suffix (-1 if it has none)
function SmoothUI_FindOption(group : name, varName : name, suffix : string) : int
{
	var config : CInGameConfigWrapper;
	var i : int;

	config = theGame.GetInGameConfigWrapper();
	for (i = 0; i < config.GetVarOptionsNum(group, varName); i += 1)
	{
		if (StrEndsWith(config.GetVarOption(group, varName, i), suffix))
		{
			return i;
		}
	}
	return -1;
}
