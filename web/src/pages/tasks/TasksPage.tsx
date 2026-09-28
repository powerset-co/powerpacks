import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type SVGProps } from "react"

import { EmptyState } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchTask, setInstalled } from "@/lib/api/tasks"
import { ago } from "@/lib/copy"
import { cn } from "@/lib/utils"
import type { Runner, Task, TaskRun } from "@/types/tasks"

const RUNNER_ORDER: readonly Runner[] = ["codex", "claude"]

const RUNNER_TITLE: Readonly<Record<Runner, string>> = { codex: "Codex", claude: "Claude" }

const STATUS: Readonly<Record<TaskRun["status"], { label: string; variant: "ok" | "warn" | "muted" }>> = {
  ok: { label: "Synced", variant: "ok" },
  failed: { label: "Needs you", variant: "warn" },
  unknown: { label: "No report", variant: "muted" },
}

const TASK_KEY = ["tasks", "task"]
const CODEX_POLL_MS = 3_000

/** The scheduled refresh as a plugin-style tile, then every run it left behind. */
export function TasksPage() {
  const client = useQueryClient()
  // Codex installs by opening a prefilled thread; the task exists once Codex creates it.
  const [awaitingCodex, setAwaitingCodex] = useState(false)
  const task = useQuery({
    queryKey: TASK_KEY,
    queryFn: ({ signal }) => fetchTask(signal),
    refetchInterval: awaitingCodex ? CODEX_POLL_MS : false,
    // The user is off in Codex sending the message, so this tab is usually hidden.
    refetchIntervalInBackground: true,
  })
  const change = useMutation({
    mutationFn: ({ runner, installed }: { runner: Runner; installed: boolean }) =>
      setInstalled(runner, installed),
    onSuccess: (next, { runner, installed }) => {
      client.setQueryData(TASK_KEY, next)
      setAwaitingCodex(runner === "codex" && installed)
    },
  })
  const codexInstalled = task.data?.installs.includes("codex") ?? false
  if (awaitingCodex && codexInstalled) setAwaitingCodex(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const runs = task.data?.runs ?? []
  const selected = runs.find((run) => run.id === selectedId) ?? runs[0]

  return (
    <main className="overflow-y-auto px-5 py-6 max-[680px]:px-4">
      <div className="mx-auto flex max-w-[1040px] flex-col gap-6">
        <h1 className="m-0 text-lg font-semibold">Scheduled tasks</h1>
        {task.error && <EmptyState>{errorText(task.error)}</EmptyState>}
        {task.isPending && <EmptyState>Loading scheduled tasks…</EmptyState>}
        {task.data && (
          <>
            <section
              aria-label="Tasks"
              className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3"
            >
              <TaskTile
                task={task.data}
                pending={change.isPending ? change.variables.runner : null}
                awaitingCodex={awaitingCodex}
                error={change.error}
                onChange={(runner, installed) => change.mutate({ runner, installed })}
              />
            </section>
            <History runs={runs} selected={selected} onSelect={setSelectedId} />
          </>
        )}
      </div>
    </main>
  )
}

interface TaskTileProps {
  task: Task
  pending: Runner | null
  awaitingCodex: boolean
  error: Error | null
  onChange: (runner: Runner, installed: boolean) => void
}

function TaskTile({ task, pending, awaitingCodex, error, onChange }: TaskTileProps) {
  const installed = new Set(task.installs)
  return (
    <article className="flex flex-col gap-3 rounded-[var(--radius-l)] border border-line bg-card p-4">
      <div>
        <h2 className="m-0 text-sm font-semibold">{task.name}</h2>
        <p className="mb-0 mt-1 text-xs leading-relaxed text-muted-foreground">
          Pulls new Gmail, iMessage and WhatsApp messages every morning at 6.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {RUNNER_ORDER.map((runner) => {
          const on = installed.has(runner)
          const busy = pending === runner
          return (
            <Button
              key={runner}
              size="sm"
              shape="pill"
              variant={on ? "ok" : "default"}
              disabled={busy}
              data-runner={runner}
              data-installed={on}
              title={runner === "claude" ? "Restarts Claude Desktop to pick up the change" : undefined}
              onClick={() => onChange(runner, !on)}
              className="group"
            >
              <RunnerIcon runner={runner} />
              {busy ? on ? "Removing…" : "Installing…" : <InstallLabel runner={runner} installed={on} />}
            </Button>
          )
        })}
      </div>
      {awaitingCodex && (
        <p className="m-0 text-xs text-muted-foreground">Send the prefilled message in Codex to finish.</p>
      )}
      {error && <p className="m-0 text-xs text-bad">{errorText(error)}</p>}
    </article>
  )
}

/** "Install", or "Installed" that reads "Remove" on hover. */
function InstallLabel({ runner, installed }: { runner: Runner; installed: boolean }) {
  if (!installed) return <>Install {RUNNER_TITLE[runner]}</>
  return (
    <>
      <span className="group-hover:hidden">{RUNNER_TITLE[runner]} ✓</span>
      <span className="hidden group-hover:inline">Remove</span>
    </>
  )
}

function RunnerIcon({ runner, ...props }: { runner: Runner } & SVGProps<SVGSVGElement>) {
  if (runner === "claude") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden className="text-[#d97757]" {...props}>
        <path
          fill="currentColor"
          d="M12 2.5l1.4 6.2 5.3-3.5-3.5 5.3 6.2 1.5-6.2 1.4 3.5 5.3-5.3-3.5L12 21.5l-1.4-6.3-5.3 3.5 3.5-5.3L2.5 12l6.3-1.5-3.5-5.3 5.3 3.5z"
        />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden fill="none" stroke="currentColor" strokeWidth={2} {...props}>
      <rect x="3" y="4" width="18" height="16" rx="4" />
      <path d="M8 10l2.5 2L8 14M13 14h3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

interface HistoryProps {
  runs: TaskRun[]
  selected: TaskRun | undefined
  onSelect: (id: string) => void
}

function History({ runs, selected, onSelect }: HistoryProps) {
  return (
    <section aria-labelledby="history" className="flex flex-col gap-3">
      <h2 id="history" className="m-0 text-[10.5px] font-semibold uppercase tracking-[.1em] text-faint">
        Run history
      </h2>
      {runs.length === 0 ? (
        <p className="m-0 text-xs text-muted-foreground">No runs yet.</p>
      ) : (
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)] gap-3 max-[820px]:grid-cols-1">
          <ol
            className="m-0 flex max-h-[560px] list-none flex-col gap-1 overflow-y-auto p-0"
            aria-label="Runs"
          >
            {runs.map((run) => (
              <li key={`${run.runner}:${run.id}`}>
                <button
                  type="button"
                  aria-pressed={run === selected}
                  onClick={() => onSelect(run.id)}
                  className="flex w-full cursor-pointer flex-col gap-1 rounded-[var(--radius-m)] border border-transparent bg-transparent px-3 py-2.5 text-left text-foreground hover:bg-card aria-pressed:border-line aria-pressed:bg-card"
                >
                  <span className="flex items-center gap-2 text-xs">
                    <RunnerIcon runner={run.runner} className="size-3.5 shrink-0" />
                    <span className="text-faint">{ago(run.started_at)}</span>
                    <Badge variant={STATUS[run.status].variant} className="ml-auto">
                      {STATUS[run.status].label}
                    </Badge>
                  </span>
                  <span className="line-clamp-1 text-xs text-muted-foreground">{firstLine(run.summary)}</span>
                </button>
              </li>
            ))}
          </ol>
          {selected && <RunDetail run={selected} />}
        </div>
      )}
    </section>
  )
}

function RunDetail({ run }: { run: TaskRun }) {
  return (
    <article className="flex flex-col gap-3 self-start rounded-[var(--radius-l)] border border-line bg-card p-4">
      <header className="flex items-center gap-2 text-xs">
        <RunnerIcon runner={run.runner} className="size-4" />
        <span className="font-semibold">{RUNNER_TITLE[run.runner]}</span>
        <span className="text-faint">{new Date(run.started_at).toLocaleString()}</span>
      </header>
      <p className="m-0 whitespace-pre-wrap text-[12.5px] leading-relaxed">
        {run.summary || "This run left no report."}
      </p>
      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        {run.open_url && (
          <a className="text-xs font-semibold text-info no-underline hover:underline" href={run.open_url}>
            Open in Codex
          </a>
        )}
        {run.resume_command && <CopyButton label="Copy resume command" text={run.resume_command} />}
      </div>
    </article>
  )
}

function CopyButton({ label, text }: { label: string; text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      size="sm"
      variant="ghost"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => setCopied(true))
      }}
      className={cn(copied && "text-ok")}
    >
      {copied ? "Copied" : label}
    </Button>
  )
}

function firstLine(text: string): string {
  return text.split("\n").find((line) => line.trim()) ?? "No report."
}
