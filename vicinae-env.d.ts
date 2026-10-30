/// <reference types="@vicinae/api">

/*
 * This file is auto-generated from the extension's manifest.
 * Do not modify manually. Instead, update the `package.json` file.
 */

type ExtensionPreferences = {
  /** Dictionary Folder - Folder scanned recursively for MDX files and matching MDD resources */
	"dictionaryFolder"?: string;

	/** Max Suggestions - Maximum number of words shown in each dictionary section */
	"maxSuggestions": string;

	/** Fuzzy Search - Include spelling-near matches when prefix results are insufficient */
	"fuzzySearch": boolean;
}

declare type Preferences = ExtensionPreferences

declare namespace Preferences {
  /** Command: Dictionary */
	export type Dictionary = ExtensionPreferences & {

	}

	/** Command: Configure Dictionaries */
	export type ConfigureDictionaries = ExtensionPreferences & {

	}

	/** Command: Lookup Selected Text */
	export type LookupSelectedText = ExtensionPreferences & {

	}
}

declare namespace Arguments {
  /** Command: Dictionary */
	export type Dictionary = {

	}

	/** Command: Configure Dictionaries */
	export type ConfigureDictionaries = {

	}

	/** Command: Lookup Selected Text */
	export type LookupSelectedText = {

	}
}