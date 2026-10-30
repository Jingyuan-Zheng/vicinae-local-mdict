import {
  Action,
  ActionPanel,
  Color,
  environment,
  getPreferenceValues,
  Icon,
  LaunchProps,
  List,
  showToast,
  Toast,
} from "@vicinae/api";
import { execFile, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { accessSync, constants, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  DictionaryConfiguration,
  loadDictionaryConfiguration,
  type StoredDictionaryConfig,
} from "./configure-dictionaries";

interface Preferences {
  dictionaryFolder: string;
  maxSuggestions: string;
  fuzzySearch: boolean;
}

interface DictionaryLaunchContext {
  query?: string;
}

interface DictionaryStatus {
  name: string;
  file: string;
  entries: number;
  mddCount: number;
}

interface BackendStatus {
  folder: string;
  dictionaries: DictionaryStatus[];
  errors: Array<{ dictionary: string; error: string }>;
}

interface Suggestion {
  word: string;
  exact: boolean;
}

interface SuggestionGroup {
  dictionary: string;
  priority: number;
  items: Suggestion[];
}

interface SelectedResult {
  id: string;
  word: string;
  dictionary: string;
}

interface DefinitionSection {
  dictionary: string;
  html: string;
  found: boolean;
  links?: Array<{ word: string; label: string }>;
  warnings?: string[];
  error?: string;
}

interface LookupResult {
  word: string;
  sections: DefinitionSection[];
  plain: string;
  pronunciation: string | null;
}

interface BackendResponse<T> {
  id: number;
  ok: boolean;
  result?: T;
  error?: string;
  trace?: string;
}

type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
};

function expandHome(value: string): string {
  if (value === "~") return process.env.HOME ?? value;
  if (value.startsWith("~/")) return join(process.env.HOME ?? "~", value.slice(2));
  return value;
}

function executable(name: string): Promise<string | null> {
  return new Promise((resolve) => {
    execFile("/usr/bin/env", ["sh", "-c", `command -v "$1"`, "sh", name], { encoding: "utf8" }, (error, stdout) => {
      resolve(error ? null : stdout.trim() || null);
    });
  });
}

function currentAppearance(fallback: "light" | "dark"): Promise<"light" | "dark"> {
  return new Promise((resolve) => {
    execFile(
      "gsettings",
      ["get", "org.gnome.desktop.interface", "color-scheme"],
      { encoding: "utf8", timeout: 250 },
      (error, stdout) => {
        if (error) {
          resolve(fallback);
          return;
        }
        const scheme = stdout.toLocaleLowerCase();
        resolve(scheme.includes("prefer-dark") ? "dark" : "light");
      },
    );
  });
}

class BackendClient {
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<number, PendingRequest>();
  private nextId = 1;
  private stdoutBuffer = "";
  private stderr = "";

  constructor(private readonly config: Record<string, unknown>) {}

  async start(): Promise<BackendStatus> {
    if (this.child) return this.request<BackendStatus>("status");
    const extensionRoot = dirname(environment.assetsPath);
    const python = join(extensionRoot, ".venv", "bin", "python");
    const helper = join(environment.assetsPath, "backend", "helper.py");
    for (const path of [python, helper]) {
      if (!existsSync(path)) throw new Error(`Missing helper component: ${path}`);
      accessSync(path, constants.R_OK);
    }
    this.child = spawn(python, [helper, "--serve"], { stdio: ["pipe", "pipe", "pipe"] });
    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");
    this.child.stdout.on("data", (chunk: string) => this.consume(chunk));
    this.child.stderr.on("data", (chunk: string) => {
      this.stderr = (this.stderr + chunk).slice(-16_384);
    });
    this.child.on("exit", (code, signal) => {
      const detail = this.stderr.trim();
      const error = new Error(`MDict helper stopped (${signal ?? code ?? "unknown"})${detail ? `: ${detail}` : ""}`);
      for (const request of this.pending.values()) request.reject(error);
      this.pending.clear();
      this.child = null;
    });
    return this.request<BackendStatus>("initialize", { config: this.config });
  }

  stop(): void {
    this.child?.kill();
    this.child = null;
  }

  request<T>(command: string, body: Record<string, unknown> = {}): Promise<T> {
    if (!this.child) return Promise.reject(new Error("MDict helper is not running"));
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
      this.child?.stdin.write(`${JSON.stringify({ id, command, ...body })}\n`);
    });
  }

  private consume(chunk: string): void {
    this.stdoutBuffer += chunk;
    for (;;) {
      const newline = this.stdoutBuffer.indexOf("\n");
      if (newline < 0) break;
      const line = this.stdoutBuffer.slice(0, newline);
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (!line.trim()) continue;
      try {
        const response = JSON.parse(line) as BackendResponse<unknown>;
        const pending = this.pending.get(response.id);
        if (!pending) continue;
        this.pending.delete(response.id);
        if (response.ok) pending.resolve(response.result);
        else pending.reject(new Error(response.error ?? "Unknown MDict backend error"));
      } catch (error) {
        this.stderr = `${this.stderr}\nInvalid helper response: ${line}\n${String(error)}`.slice(-16_384);
      }
    }
  }
}

function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function itemId(dictionary: string, word: string): string {
  return `${encodeURIComponent(dictionary)}::${encodeURIComponent(word)}`;
}

function orderGroups(groups: SuggestionGroup[], priorityDictionary: string): SuggestionGroup[] {
  return [...groups].sort((left, right) => {
    const leftPreferred = left.dictionary === priorityDictionary ? 0 : 1;
    const rightPreferred = right.dictionary === priorityDictionary ? 0 : 1;
    return leftPreferred - rightPreferred || left.priority - right.priority || left.dictionary.localeCompare(right.dictionary);
  });
}

// List.Item.Detail currently names this transport property `markdown`, but its
// renderer passes inline HTML to Qt Rich Text. Each zero-width-prefixed paragraph
// below contains raw, sanitized dictionary HTML rather than Markdown markup.
function buildDetailHtml(result: LookupResult | null, loading: boolean, status: BackendStatus | null): string {
  if (loading && !result) return '\u200B<div style="font-size:20px;font-weight:600">Loading…</div><div>Searching enabled dictionaries.</div>';
  if (!result) {
    const count = status?.dictionaries.length ?? 0;
    return `\u200B<div style="font-size:24px;font-weight:700">Local MDict Dictionary</div><div>Type a word to search ${count} loaded ${count === 1 ? "dictionary" : "dictionaries"}.</div>`;
  }
  const blocks = [`\u200B<div style="font-size:26px;font-weight:700;margin-bottom:8px">${escapeHtml(result.word)}</div>`];
  for (const [index, section] of result.sections.entries()) {
    if (!section.found && !section.error) continue;
    const title = `${index === 0 ? "⭐ " : ""}${escapeHtml(section.dictionary)}${index === 0 ? " · Priority" : ""}`;
    let content = section.html;
    if (section.error) content = `<div style="color:#d44">Dictionary error: ${escapeHtml(section.error)}</div>`;
    else if (!section.found) content = '<div style="opacity:.65;font-style:italic">No exact entry.</div>';
    blocks.push(
      `\u200B<section><div style="font-size:20px;font-weight:700;margin:10px 0 8px">${title}</div>${content}</section>`,
    );
  }
  // Blank lines deliberately produce separate Qt rich-text documents, keeping
  // one dictionary's selectors from restyling another dictionary's definition.
  return blocks.join("\n\n");
}

async function playAudio(path: string): Promise<void> {
  const candidates: Array<[string, string[]]> = [
    ["pw-play", [path]],
    ["paplay", [path]],
    ["mpv", ["--no-video", "--really-quiet", path]],
    ["ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet", path]],
    ["xdg-open", [path]],
  ];
  for (const [name, args] of candidates) {
    const binary = await executable(name);
    if (binary) {
      const child = spawn(binary, args, { detached: true, stdio: "ignore" });
      child.unref();
      return;
    }
  }
  throw new Error("No supported audio player was found");
}

export function DictionaryCommand(
  props: LaunchProps<{ arguments: Record<string, string>; launchContext: DictionaryLaunchContext }>,
) {
  const preferences = getPreferenceValues<Preferences>();
  const [dictionaryConfiguration, setDictionaryConfiguration] = useState<StoredDictionaryConfig | null>(null);
  useEffect(() => {
    loadDictionaryConfiguration().then(setDictionaryConfiguration).catch(() => setDictionaryConfiguration({ order: [], disabled: [] }));
  }, []);
  const config = useMemo(
    () => ({
      dictionaryFolder: expandHome(preferences.dictionaryFolder || "~/Documents/Dictionaries"),
      disabledDictionaries: dictionaryConfiguration?.disabled ?? [],
      orderedDictionaries: dictionaryConfiguration?.order ?? [],
      maxSuggestions: Number.parseInt(preferences.maxSuggestions || "30", 10) || 30,
      fuzzySearch: preferences.fuzzySearch !== false,
      appearance: environment.appearance,
      cacheDir: join(environment.supportPath, "mdict-cache"),
    }),
    [preferences.dictionaryFolder, preferences.maxSuggestions, preferences.fuzzySearch, dictionaryConfiguration],
  );
  const clientRef = useRef<BackendClient | null>(null);
  const requestGeneration = useRef(0);
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [query, setQuery] = useState(() => (props.launchContext?.query ?? props.fallbackText ?? "").trim());
  const [suggestionGroups, setSuggestionGroups] = useState<SuggestionGroup[]>([]);
  const [selected, setSelected] = useState<SelectedResult | null>(null);
  const [priorityDictionary, setPriorityDictionary] = useState("");
  const [lookup, setLookup] = useState<LookupResult | null>(null);
  const [starting, setStarting] = useState(true);
  const [searching, setSearching] = useState(false);
  const [definitionLoading, setDefinitionLoading] = useState(false);
  const [fatalError, setFatalError] = useState<string | null>(null);

  useEffect(() => {
    if (dictionaryConfiguration === null) return;
    setStarting(true);
    setFatalError(null);
    const client = new BackendClient(config);
    clientRef.current = client;
    client
      .start()
      .then(setStatus)
      .catch((error: Error) => setFatalError(error.message))
      .finally(() => setStarting(false));
    return () => client.stop();
  }, [config, dictionaryConfiguration]);

  useEffect(() => {
    if (status?.dictionaries.length && !status.dictionaries.some((dictionary) => dictionary.name === priorityDictionary)) {
      setPriorityDictionary(status.dictionaries[0].name);
    }
  }, [status, priorityDictionary]);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    if (!status || !query.trim()) {
      setSuggestionGroups([]);
      setSelected(null);
      setLookup(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const timer = setTimeout(() => {
      clientRef.current
        ?.request<SuggestionGroup[]>("suggest", { query })
        .then((groups) => {
          if (generation !== requestGeneration.current) return;
          setSuggestionGroups(groups);
          const ordered = orderGroups(groups, priorityDictionary);
          const choices = ordered.flatMap((group) =>
            group.items.map((item) => ({ id: itemId(group.dictionary, item.word), word: item.word, dictionary: group.dictionary, exact: item.exact })),
          );
          const next = choices.find((item) => item.exact) ?? choices[0];
          // A previous prefix result (for example "Knowles") may still be in
          // the refreshed list after the user finishes typing "know". Always
          // move to the new query's exact entry instead of preserving that
          // stale selection and looking up the wrong word in every dictionary.
          setSelected(next ?? {
            id: itemId(priorityDictionary || status.dictionaries[0]?.name || "All", query.trim()),
            word: query.trim(),
            dictionary: priorityDictionary || status.dictionaries[0]?.name || "",
          });
        })
        .catch((error: Error) => setFatalError(error.message))
        .finally(() => generation === requestGeneration.current && setSearching(false));
    }, 35);
    return () => clearTimeout(timer);
  }, [query, status, priorityDictionary]);

  useEffect(() => {
    if (!selected || !status) {
      setLookup(null);
      return;
    }
    let active = true;
    setLookup(null);
    setDefinitionLoading(true);
    // Keep suggestions on the persistent index process. A separate disposable
    // process renders definitions so an expensive stale lookup can be killed
    // immediately and never blocks the user's next keystroke.
    let client: BackendClient | null = null;
    void (async () => {
      try {
        // Updating Detail first with one dictionary and then expanding it with
        // every dictionary makes Qt preserve a bottom scroll anchor. Submit a
        // single, complete document so a new search always opens at the top.
        const appearance = await currentAppearance(environment.appearance);
        if (!active) return;
        client = new BackendClient({ ...config, appearance });
        await client.start();
        const completeResult = await client.request<LookupResult>("lookup", {
          word: selected.word,
          preferredDictionary: selected.dictionary,
        });
        if (!active) return;
        if (completeResult) setLookup(completeResult);
      } catch (error) {
        if (active) setFatalError((error as Error).message);
      } finally {
        if (active) setDefinitionLoading(false);
        client?.stop();
      }
    })();
    return () => {
      active = false;
      client?.stop();
    };
  }, [selected, status, config]);

  const reload = async () => {
    try {
      setStarting(true);
      const next = await clientRef.current?.request<BackendStatus>("reload", { config });
      if (next) setStatus(next);
      setLookup(null);
      setQuery((value) => `${value} ` .trimEnd());
      await showToast({ style: Toast.Style.Success, title: "Dictionaries reloaded" });
    } catch (error) {
      await showToast({ style: Toast.Style.Failure, title: "Reload failed", message: String(error) });
    } finally {
      setStarting(false);
    }
  };

  const openGoldenDict = async (word: string) => {
    const binary = (await executable("goldendict-ng")) ?? (await executable("goldendict"));
    if (!binary) {
      await showToast({ style: Toast.Style.Failure, title: "GoldenDict-ng is not installed" });
      return;
    }
    const child = spawn(binary, [word], { detached: true, stdio: "ignore" });
    child.unref();
  };

  const linkedWords = useMemo(() => {
    const seen = new Set<string>();
    return (lookup?.sections ?? [])
      .flatMap((section) => (section.links ?? []).map((link) => ({ ...link, dictionary: section.dictionary })))
      .filter((link) => {
        const key = link.word.toLocaleLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 80);
  }, [lookup]);

  const lookupLinkedWord = (word: string) => {
    setSelected(null);
    setLookup(null);
    setQuery(word);
  };

  const orderedGroups = orderGroups(suggestionGroups, priorityDictionary);
  const detailHtml = buildDetailHtml(lookup, definitionLoading, status);
  const detailKey = `${selected?.id ?? query.trim()}:${lookup?.sections.map((section) => section.dictionary).join("|") ?? "loading"}`;
  const currentWord = selected?.word ?? query.trim();

  return (
    <List
      isLoading={starting || searching}
      isShowingDetail
      searchBarPlaceholder="Type a word…"
      searchText={query}
      onSearchTextChange={setQuery}
      onSelectionChange={(id) => {
        for (const group of suggestionGroups) {
          const item = group.items.find((candidate) => itemId(group.dictionary, candidate.word) === id);
          if (item) {
            setSelected({ id, word: item.word, dictionary: group.dictionary });
            return;
          }
        }
      }}
      searchBarAccessory={
        status?.dictionaries.length ? (
          <List.Dropdown tooltip="Priority Dictionary" value={priorityDictionary} onChange={setPriorityDictionary}>
            {status.dictionaries.map((dictionary, index) => (
              <List.Dropdown.Item key={dictionary.name} value={dictionary.name} title={`${index + 1}. ${dictionary.name}`} />
            ))}
          </List.Dropdown>
        ) : undefined
      }
    >
      {fatalError ? (
        <List.EmptyView title="Local MDict could not start" description={fatalError} icon={{ source: Icon.Warning, tintColor: Color.Red }} />
      ) : null}
      {!fatalError && !query.trim() ? (
        <List.EmptyView
          title={status?.dictionaries.length ? "Search local dictionaries" : "No dictionaries loaded"}
          description={
            status?.dictionaries.length
              ? `${status.dictionaries.length} dictionaries loaded from ${status.folder}`
              : `Put MDX/MDD files in ${status?.folder ?? config.dictionaryFolder}`
          }
          icon={Icon.Book}
        />
      ) : null}
      {orderedGroups.map((group, index) => (
        <List.Section
          key={group.dictionary}
          title={`${index === 0 ? "⭐ " : ""}${group.dictionary}`}
          subtitle={index === 0 ? "Priority dictionary" : `Priority ${index + 1}`}
        >
          {group.items.map((item) => {
            const id = itemId(group.dictionary, item.word);
            return (
              <DictionaryItem
                key={id}
                id={id}
                isSelected={selected?.id === id}
                item={item}
                detailHtml={detailHtml}
                detailKey={detailKey}
                lookup={lookup}
                onReload={reload}
                onOpenGoldenDict={openGoldenDict}
                linkedWords={linkedWords}
                onLookupLinkedWord={lookupLinkedWord}
                dictionaryFolder={config.dictionaryFolder}
                onConfigurationChanged={setDictionaryConfiguration}
              />
            );
          })}
        </List.Section>
      ))}
      {!fatalError && query.trim() && !suggestionGroups.length && !searching ? (
        <List.Item
          id={itemId(priorityDictionary || "All", query.trim())}
          title={query.trim()}
          icon={Icon.MagnifyingGlass}
          detail={<List.Item.Detail key={detailKey} markdown={detailHtml} />}
          actions={<DictionaryActions word={currentWord} definition={lookup?.plain ?? ""} pronunciation={lookup?.pronunciation ?? null} linkedWords={linkedWords} onLookupLinkedWord={lookupLinkedWord} onReload={reload} onOpenGoldenDict={openGoldenDict} dictionaryFolder={config.dictionaryFolder} onConfigurationChanged={setDictionaryConfiguration} />}
        />
      ) : null}
    </List>
  );
}

export default DictionaryCommand;

function DictionaryItem({
  id,
  isSelected,
  item,
  detailHtml,
  detailKey,
  lookup,
  onReload,
  onOpenGoldenDict,
  linkedWords,
  onLookupLinkedWord,
  dictionaryFolder,
  onConfigurationChanged,
}: {
  id: string;
  isSelected: boolean;
  item: Suggestion;
  detailHtml: string;
  detailKey: string;
  lookup: LookupResult | null;
  onReload: () => Promise<void>;
  onOpenGoldenDict: (word: string) => Promise<void>;
  linkedWords: Array<{ word: string; label: string; dictionary: string }>;
  onLookupLinkedWord: (word: string) => void;
  dictionaryFolder: string;
  onConfigurationChanged: (config: StoredDictionaryConfig) => void;
}) {
  return (
    <List.Item
      id={id}
      title={item.word}
      icon={item.exact ? Icon.CheckCircle : Icon.Book}
      detail={<List.Item.Detail key={isSelected ? detailKey : `${id}:idle`} markdown={isSelected ? detailHtml : "\u200B"} />}
      actions={
        <DictionaryActions
          word={item.word}
          definition={isSelected && lookup?.word === item.word ? lookup.plain : ""}
          pronunciation={isSelected && lookup?.word === item.word ? lookup.pronunciation : null}
          onReload={onReload}
          onOpenGoldenDict={onOpenGoldenDict}
          linkedWords={isSelected ? linkedWords : []}
          onLookupLinkedWord={onLookupLinkedWord}
          dictionaryFolder={dictionaryFolder}
          onConfigurationChanged={onConfigurationChanged}
        />
      }
    />
  );
}

function DictionaryActions({
  word,
  definition,
  pronunciation,
  onReload,
  onOpenGoldenDict,
  linkedWords,
  onLookupLinkedWord,
  dictionaryFolder,
  onConfigurationChanged,
}: {
  word: string;
  definition: string;
  pronunciation: string | null;
  onReload: () => Promise<void>;
  onOpenGoldenDict: (word: string) => Promise<void>;
  linkedWords: Array<{ word: string; label: string; dictionary: string }>;
  onLookupLinkedWord: (word: string) => void;
  dictionaryFolder: string;
  onConfigurationChanged: (config: StoredDictionaryConfig) => void;
}) {
  return (
    <ActionPanel>
      <Action.CopyToClipboard title="Copy Word" content={word} icon={Icon.CopyClipboard} />
      <Action.CopyToClipboard title="Copy Definition" content={definition || word} icon={Icon.BlankDocument} />
      {linkedWords.length ? (
        <ActionPanel.Submenu title="Look Up Linked Word" icon={Icon.Link}>
          {linkedWords.map((link) => (
            <Action
              key={`${link.dictionary}:${link.word}`}
              title={link.label === link.word ? link.word : `${link.label} — ${link.word}`}
              icon={Icon.MagnifyingGlass}
              onAction={() => onLookupLinkedWord(link.word)}
            />
          ))}
        </ActionPanel.Submenu>
      ) : null}
      {pronunciation ? (
        <Action
          title="Play Pronunciation"
          icon={Icon.SpeakerHigh}
          onAction={async () => {
            try {
              await playAudio(pronunciation);
            } catch (error) {
              await showToast({ style: Toast.Style.Failure, title: "Could not play pronunciation", message: String(error) });
            }
          }}
        />
      ) : null}
      <Action title="Open in GoldenDict-ng" icon={Icon.AppWindow} onAction={() => onOpenGoldenDict(word)} />
      <Action.Push
        title="Configure Dictionaries"
        icon={Icon.Cog}
        target={<DictionaryConfiguration folder={dictionaryFolder} onSaved={onConfigurationChanged} />}
      />
      <Action title="Reload Dictionaries" icon={Icon.ArrowClockwise} shortcut={{ modifiers: ["ctrl", "shift"], key: "r" }} onAction={onReload} />
    </ActionPanel>
  );
}
