import { getSelectedText, Icon, LaunchProps, List } from "@vicinae/api";
import { useEffect, useState } from "react";
import { DictionaryCommand } from "./dictionary";

type SelectionState =
  | { status: "loading" }
  | { status: "ready"; query: string }
  | { status: "error"; message: string };

const wait = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function readSelectedText(): Promise<string> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      const value = (await getSelectedText()).replace(/\s+/gu, " ").trim();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await wait(100);
  }
  throw lastError instanceof Error ? lastError : new Error("Unable to get selected text");
}

export default function LookupSelectedTextCommand(props: LaunchProps) {
  const [selection, setSelection] = useState<SelectionState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    readSelectedText()
      .then((query) => {
        if (!active) return;
        if (query.length > 200) setSelection({ status: "error", message: "The selected text is too long to look up." });
        else setSelection({ status: "ready", query });
      })
      .catch((error) => {
        if (active) {
          setSelection({
            status: "error",
            message: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      active = false;
    };
  }, []);

  if (selection.status === "ready") {
    return (
      <DictionaryCommand
        {...props}
        arguments={props.arguments ?? {}}
        launchContext={{ ...(props.launchContext ?? {}), query: selection.query }}
      />
    );
  }

  return (
    <List isLoading={selection.status === "loading"} searchBarPlaceholder="Reading selected text…">
      {selection.status === "error" ? (
        <List.EmptyView title="No selected text" description={selection.message} icon={Icon.TextSelection} />
      ) : null}
    </List>
  );
}
