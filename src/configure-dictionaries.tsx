import { Action, ActionPanel, Color, getPreferenceValues, Icon, List, LocalStorage, showToast, Toast } from "@vicinae/api";
import { readdir } from "node:fs/promises";
import { join, relative } from "node:path";
import { useEffect, useMemo, useState } from "react";

export const DICTIONARY_CONFIG_KEY = "local-mdict-dictionary-selection-v1";

export interface StoredDictionaryConfig {
  order: string[];
  disabled: string[];
}

interface Preferences {
  dictionaryFolder: string;
}

interface DictionaryFile {
  path: string;
  name: string;
  location: string;
}

function expandHome(value: string): string {
  if (value === "~") return process.env.HOME ?? value;
  if (value.startsWith("~/")) return join(process.env.HOME ?? "~", value.slice(2));
  return value;
}

export async function loadDictionaryConfiguration(): Promise<StoredDictionaryConfig> {
  const raw = await LocalStorage.getItem<string>(DICTIONARY_CONFIG_KEY);
  if (!raw) return { order: [], disabled: [] };
  try {
    const value = JSON.parse(raw) as Partial<StoredDictionaryConfig>;
    return {
      order: Array.isArray(value.order) ? value.order.filter((item): item is string => typeof item === "string") : [],
      disabled: Array.isArray(value.disabled) ? value.disabled.filter((item): item is string => typeof item === "string") : [],
    };
  } catch {
    return { order: [], disabled: [] };
  }
}

async function scanMdx(folder: string): Promise<DictionaryFile[]> {
  const found: DictionaryFile[] = [];
  async function visit(directory: string): Promise<void> {
    const entries = await readdir(directory, { withFileTypes: true });
    await Promise.all(
      entries.map(async (entry) => {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) await visit(path);
        else if (entry.isFile() && entry.name.toLocaleLowerCase().endsWith(".mdx")) {
          found.push({ path, name: entry.name.replace(/\.mdx$/i, ""), location: relative(folder, path) });
        }
      }),
    );
  }
  await visit(folder);
  return found;
}

export function DictionaryConfiguration({
  folder,
  onSaved,
}: {
  folder: string;
  onSaved?: (config: StoredDictionaryConfig) => void;
}) {
  const [files, setFiles] = useState<DictionaryFile[]>([]);
  const [disabled, setDisabled] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    Promise.all([scanMdx(folder), loadDictionaryConfiguration()])
      .then(([discovered, saved]) => {
        if (!active) return;
        const byPath = new Map(discovered.map((file) => [file.path, file]));
        const ordered = saved.order.flatMap((path) => (byPath.has(path) ? [byPath.get(path)!] : []));
        const known = new Set(ordered.map((file) => file.path));
        ordered.push(...discovered.filter((file) => !known.has(file.path)).sort((a, b) => a.location.localeCompare(b.location)));
        setFiles(ordered);
        setDisabled(new Set(saved.disabled.filter((path) => byPath.has(path))));
      })
      .catch(async (error) => {
        await showToast({ style: Toast.Style.Failure, title: "Could not scan dictionaries", message: String(error) });
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [folder]);

  const save = async (nextFiles: DictionaryFile[], nextDisabled: Set<string>) => {
    const value = { order: nextFiles.map((file) => file.path), disabled: [...nextDisabled] };
    setFiles(nextFiles);
    setDisabled(nextDisabled);
    await LocalStorage.setItem(DICTIONARY_CONFIG_KEY, JSON.stringify(value));
    onSaved?.(value);
  };

  const enabledFiles = useMemo(() => files.filter((file) => !disabled.has(file.path)), [files, disabled]);
  const disabledFiles = useMemo(() => files.filter((file) => disabled.has(file.path)), [files, disabled]);

  const actions = (file: DictionaryFile) => {
    const isDisabled = disabled.has(file.path);
    const enabledIndex = enabledFiles.findIndex((candidate) => candidate.path === file.path);
    const move = async (offset: number) => {
      const currentIndex = files.findIndex((candidate) => candidate.path === file.path);
      const otherEnabled = enabledFiles[enabledIndex + offset];
      if (!otherEnabled) return;
      const otherIndex = files.findIndex((candidate) => candidate.path === otherEnabled.path);
      const next = [...files];
      [next[currentIndex], next[otherIndex]] = [next[otherIndex], next[currentIndex]];
      await save(next, disabled);
    };
    return (
      <ActionPanel>
        <Action
          title={isDisabled ? "Enable Dictionary" : "Disable Dictionary"}
          icon={isDisabled ? Icon.CheckCircle : Icon.XMarkCircle}
          onAction={() => {
            const next = new Set(disabled);
            if (isDisabled) next.delete(file.path);
            else next.add(file.path);
            void save(files, next);
          }}
        />
        {!isDisabled && enabledIndex > 0 ? <Action title="Move Up" icon={Icon.ArrowUp} onAction={() => void move(-1)} /> : null}
        {!isDisabled && enabledIndex < enabledFiles.length - 1 ? <Action title="Move Down" icon={Icon.ArrowDown} onAction={() => void move(1)} /> : null}
        {!isDisabled && enabledIndex > 0 ? (
          <Action
            title="Make Priority Dictionary"
            icon={Icon.Star}
            onAction={() => {
              const next = [file, ...files.filter((candidate) => candidate.path !== file.path)];
              void save(next, disabled);
            }}
          />
        ) : null}
      </ActionPanel>
    );
  };

  return (
    <List isLoading={loading} searchBarPlaceholder="Filter discovered dictionaries…">
      <List.Section title="Enabled — lookup order" subtitle={`${enabledFiles.length} dictionaries`}>
        {enabledFiles.map((file, index) => (
          <List.Item
            key={file.path}
            title={file.name}
            subtitle={file.location}
            icon={{ source: index === 0 ? Icon.Star : Icon.Book, tintColor: index === 0 ? Color.Yellow : undefined }}
            accessories={[{ text: `#${index + 1}` }]}
            actions={actions(file)}
          />
        ))}
      </List.Section>
      <List.Section title="Disabled" subtitle={`${disabledFiles.length} dictionaries`}>
        {disabledFiles.map((file) => (
          <List.Item key={file.path} title={file.name} subtitle={file.location} icon={Icon.XMarkCircle} actions={actions(file)} />
        ))}
      </List.Section>
    </List>
  );
}

export default function ConfigureDictionariesCommand() {
  const preferences = getPreferenceValues<Preferences>();
  return <DictionaryConfiguration folder={expandHome(preferences.dictionaryFolder || "~/Documents/Dictionaries")} />;
}
