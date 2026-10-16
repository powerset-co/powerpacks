import { Spinner } from "@/components/shared"
import { preparedSearch, ranSearch, type PreparedSearch } from "@/lib/agent/preparedSearch"
import { cn } from "@/lib/utils"
import type { Entry, ItemStatus } from "@/types/agent"

import { CopyButton } from "./CopyButton"
import { CheckIcon, CrossIcon, FileIcon, SparkIcon, TerminalIcon } from "./icons"
import { Markdown } from "./Markdown"
import { SearchCard } from "./SearchCard"

type WorkEntry = Extract<Entry, { kind: "command" | "files" | "tool" | "reasoning" }>
type Turn =
  | { kind: "say"; entry: Exclude<Entry, WorkEntry> }
  | { kind: "work"; id: string; entries: WorkEntry[] }
  | { kind: "search"; id: string; search: PreparedSearch }

function isWork(entry: Entry): entry is WorkEntry {
  return (
    entry.kind === "command" || entry.kind === "files" || entry.kind === "tool" || entry.kind === "reasoning"
  )
}

/**
 * Consecutive work (commands, edits, tools, thinking) folds into one panel between messages. A
 * command that prepared a search is a search card in its place, between the panels around it.
 */
function group(entries: Entry[]): Turn[] {
  const out: Turn[] = []
  for (const entry of entries) {
    const last = out.at(-1)
    const search = preparedSearch(entry)
    if (search) out.push({ kind: "search", id: entry.id, search })
    else if (!isWork(entry)) out.push({ kind: "say", entry })
    else if (last?.kind === "work") last.entries.push(entry)
    else out.push({ kind: "work", id: entry.id, entries: [entry] })
  }
  return out
}

function seconds(durationMs: number | null): string {
  if (durationMs === null) return ""
  return durationMs < 1000 ? `${durationMs}ms` : `${(durationMs / 1000).toFixed(1)}s`
}

/** `/bin/zsh -lc '…'` says nothing to a reader: the command inside it, its first line, home as ~. */
function commandLabel(command: string): string {
  const inner = /^\/bin\/(?:z|ba)?sh -lc (['"])([\s\S]*)\1$/.exec(command)?.[2] ?? command
  return (inner.split("\n")[0] ?? "").replace(/\/Users\/[^/\s]+/g, "~").trim()
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`
}

/** A folded panel's one line: what the work did, and how long its commands took. */
function workSummary(entries: WorkEntry[]): string {
  let commands = 0
  let files = 0
  let tools = 0
  let ms = 0
  for (const entry of entries) {
    if (entry.kind === "command") {
      commands += 1
      ms += entry.durationMs ?? 0
    } else if (entry.kind === "files") files += entry.paths.length
    else if (entry.kind === "tool") tools += 1
  }
  const did = [
    commands ? `ran ${plural(commands, "command")}` : "",
    files ? `edited ${plural(files, "file")}` : "",
    tools ? `used ${plural(tools, "tool")}` : "",
  ].filter(Boolean)
  if (did.length === 0) return "Thought it through"
  const line = did.join(", ")
  return `${line.charAt(0).toUpperCase()}${line.slice(1)}${ms >= 1000 ? ` · ${seconds(ms)}` : ""}`
}

function StatusMark({ status }: { status: ItemStatus }) {
  if (status === "running") return <Spinner className="text-muted-foreground" />
  if (status === "done") return <CheckIcon className="size-3.5 shrink-0 text-ok" />
  return <CrossIcon className="size-3.5 shrink-0 text-bad" />
}

const SUMMARY =
  "flex cursor-pointer list-none items-center gap-2.5 px-3 py-2 text-xs transition-colors duration-fast ease-out hover:bg-surface-2 [&::-webkit-details-marker]:hidden"

// A turn's work folded to one quiet line, its icon on the text's edge; the commands open in a box
// under it.
const WORK_LINE =
  "-ml-1.5 flex w-fit max-w-full cursor-pointer list-none items-center gap-2 rounded-[var(--radius-s)] px-1.5 py-1 text-[13px] text-muted-foreground transition-colors duration-fast ease-out hover:bg-surface-2 hover:text-foreground [&::-webkit-details-marker]:hidden"

function WorkRow({ entry }: { entry: WorkEntry }) {
  switch (entry.kind) {
    case "command":
      return (
        <details className="group">
          <summary className={SUMMARY}>
            <TerminalIcon className="size-3.5 shrink-0 text-faint" />
            <code
              title={entry.command}
              className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground"
            >
              {commandLabel(entry.command)}
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
        <div className="rise-in group -mb-4 flex flex-col text-[15px] leading-[1.65]">
          <div className="max-w-[82%] self-end whitespace-pre-wrap rounded-[var(--radius-l)] rounded-br-[var(--radius-s)] border border-line bg-surface-2 px-3.5 py-2.5">
            {entry.text}
          </div>
          <CopyButton text={entry.text} className={cn(COPY, "self-end")} />
        </div>
      )
    case "agent":
      return (
        <div className="rise-in group -mb-4 flex flex-col text-[15px]">
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

export function Transcript({ entries, running }: { entries: Entry[]; running: boolean }) {
  const turns = group(entries)
  // Only the newest search card asks Execute or Modify, while the agent waits and until the
  // search ran after it (its run printed a result): then the card is the search's record. Other
  // commands after the card (logging an edit beside the prepare) leave it asking.
  const newest = turns.filter((turn) => turn.kind === "search").at(-1)
  const ranSince = turns
    .slice(newest ? turns.indexOf(newest) + 1 : turns.length)
    .some((turn) => turn.kind === "work" && turn.entries.some(ranSearch))
  const asking = !running && !ranSince
  return (
    <>
      {turns.map((turn) => {
        if (turn.kind === "say") return <Say key={turn.entry.id} entry={turn.entry} />
        if (turn.kind === "search")
          return <SearchCard key={turn.id} search={turn.search} actions={asking && turn === newest} />
        return (
          // Folded to one line until the reader asks for the commands behind it.
          <details key={turn.id} className="rise-in group/work">
            <summary className={WORK_LINE}>
              <TerminalIcon className="size-3.5 shrink-0 text-faint" />
              <span className="min-w-0 truncate">{workSummary(turn.entries)}</span>
              {turn.entries.some((entry) => "status" in entry && entry.status === "running") ? (
                <Spinner className="text-muted-foreground" />
              ) : null}
              <svg
                viewBox="0 0 16 16"
                aria-hidden="true"
                className="size-3 shrink-0 text-faint transition-transform duration-fast group-open/work:rotate-180"
              >
                <path
                  d="M4 6l4 4 4-4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </summary>
            <ol
              aria-label="Work"
              className="m-0 mt-2 list-none divide-y divide-line overflow-hidden rounded-[var(--radius-m)] border border-line bg-card p-0"
            >
              {turn.entries.map((entry) => (
                <li key={entry.id}>
                  <WorkRow entry={entry} />
                </li>
              ))}
            </ol>
          </details>
        )
      })}
    </>
  )
}
