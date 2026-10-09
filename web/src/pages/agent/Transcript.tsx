import { Spinner } from "@/components/shared"
import { cn } from "@/lib/utils"
import type { Entry, ItemStatus } from "@/types/agent"

import { CopyButton } from "./CopyButton"
import { CheckIcon, CrossIcon, FileIcon, SparkIcon, TerminalIcon } from "./icons"
import { Markdown } from "./Markdown"

type WorkEntry = Extract<Entry, { kind: "command" | "files" | "tool" | "reasoning" }>
type Turn =
  { kind: "say"; entry: Exclude<Entry, WorkEntry> } | { kind: "work"; id: string; entries: WorkEntry[] }

function isWork(entry: Entry): entry is WorkEntry {
  return (
    entry.kind === "command" || entry.kind === "files" || entry.kind === "tool" || entry.kind === "reasoning"
  )
}

/** Consecutive work (commands, edits, tools, thinking) folds into one panel between messages. */
function group(entries: Entry[]): Turn[] {
  const out: Turn[] = []
  for (const entry of entries) {
    const last = out.at(-1)
    if (!isWork(entry)) out.push({ kind: "say", entry })
    else if (last?.kind === "work") last.entries.push(entry)
    else out.push({ kind: "work", id: entry.id, entries: [entry] })
  }
  return out
}

function seconds(durationMs: number | null): string {
  if (durationMs === null) return ""
  return durationMs < 1000 ? `${durationMs}ms` : `${(durationMs / 1000).toFixed(1)}s`
}

function StatusMark({ status }: { status: ItemStatus }) {
  if (status === "running") return <Spinner className="text-muted-foreground" />
  if (status === "done") return <CheckIcon className="size-3.5 shrink-0 text-ok" />
  return <CrossIcon className="size-3.5 shrink-0 text-bad" />
}

const SUMMARY =
  "flex cursor-pointer list-none items-center gap-2.5 px-3 py-2 text-xs transition-colors duration-fast ease-out hover:bg-surface-2 [&::-webkit-details-marker]:hidden"

function WorkRow({ entry }: { entry: WorkEntry }) {
  switch (entry.kind) {
    case "command":
      return (
        <details className="group">
          <summary className={SUMMARY}>
            <TerminalIcon className="size-3.5 shrink-0 text-faint" />
            <code className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground">
              {entry.command}
            </code>
            <span className="tabular-nums text-faint">
              {entry.status === "failed" && entry.exitCode !== null ? `exit ${entry.exitCode} · ` : ""}
              {seconds(entry.durationMs)}
            </span>
            <StatusMark status={entry.status} />
          </summary>
          <pre className="m-0 max-h-72 overflow-auto border-t border-line bg-background px-3 py-2.5 font-mono text-[11.5px] leading-[1.55] text-muted-foreground">
            {entry.output.trim() || "No output."}
          </pre>
        </details>
      )
    case "files":
      return (
        <details>
          <summary className={SUMMARY}>
            <FileIcon className="size-3.5 shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate">
              Edited {entry.paths.length === 1 ? entry.paths[0] : `${entry.paths.length} files`}
            </span>
            <StatusMark status={entry.status} />
          </summary>
          <ul className="m-0 list-none border-t border-line bg-background px-3 py-2 font-mono text-[11.5px] text-muted-foreground">
            {entry.paths.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
        </details>
      )
    case "tool":
      return (
        <div className={cn(SUMMARY, "cursor-default hover:bg-transparent")}>
          <SparkIcon className="size-3.5 shrink-0 text-faint" />
          <span className="min-w-0 flex-1 truncate">{entry.label}</span>
          <StatusMark status={entry.status} />
        </div>
      )
    case "reasoning":
      if (!entry.text.trim()) return null
      return (
        <details>
          <summary className={cn(SUMMARY, "text-muted-foreground")}>
            <SparkIcon className="size-3.5 shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate italic">
              {entry.text.split("\n")[0]?.replace(/\*\*/g, "")}
            </span>
          </summary>
          <div className="border-t border-line px-3 py-2.5 text-xs text-muted-foreground">
            <Markdown text={entry.text} />
          </div>
        </details>
      )
  }
}

// The copy button sits under the message at the edge it is aligned to, and shows on hover or focus.
// Its row borrows from the gap below so messages keep their spacing.
const COPY = "mt-1 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"

function Say({ entry }: { entry: Exclude<Entry, WorkEntry> }) {
  switch (entry.kind) {
    case "user":
      return (
        <div className="rise-in group -mb-4 flex flex-col">
          <div className="max-w-[82%] self-end whitespace-pre-wrap rounded-[var(--radius-l)] rounded-br-[var(--radius-s)] border border-line bg-surface-2 px-3.5 py-2.5">
            {entry.text}
          </div>
          <CopyButton text={entry.text} className={cn(COPY, "self-end")} />
        </div>
      )
    case "agent":
      return (
        <div className="rise-in group -mb-4 flex flex-col text-[13.5px]">
          <Markdown text={entry.text} />
          <CopyButton text={entry.text} className={cn(COPY, "-ml-1.5 self-start")} />
        </div>
      )
    case "error":
      return (
        <p role="alert" className="m-0 rounded-[var(--radius-m)] bg-bad-soft px-3.5 py-2.5 text-xs text-bad">
          {entry.text}
        </p>
      )
  }
}

export function Transcript({ entries }: { entries: Entry[] }) {
  return (
    <>
      {group(entries).map((turn) =>
        turn.kind === "say" ? (
          <Say key={turn.entry.id} entry={turn.entry} />
        ) : (
          <ol
            key={turn.id}
            aria-label="Work"
            className="rise-in m-0 list-none divide-y divide-line overflow-hidden rounded-[var(--radius-m)] border border-line bg-card p-0"
          >
            {turn.entries.map((entry) => (
              <li key={entry.id}>
                <WorkRow entry={entry} />
              </li>
            ))}
          </ol>
        ),
      )}
    </>
  )
}
