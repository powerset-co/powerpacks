import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type SVGProps } from "react"

import { EmptyState } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { fetchTask, setInstalled } from "@/lib/api/tasks"
import { ago } from "@/lib/copy"
import { cn } from "@/lib/utils"
import type { Runner, ScheduleSettings, Task, TaskRun } from "@/types/tasks"

import { ScheduleDialog } from "./ScheduleDialog"

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
  const task = useQuery({
    queryKey: TASK_KEY,
    queryFn: ({ signal }) => fetchTask(signal),
    refetchInterval: (query) =>
      query.state.data?.codex_install_status === "pending" ? CODEX_POLL_MS : false,
    refetchIntervalInBackground: true,
  })
  const change = useMutation({
    mutationFn: ({
      runner,
      installed,
      schedule,
    }: {
      runner: Runner
      installed: boolean
      schedule?: ScheduleSettings
    }) => setInstalled(runner, installed, schedule),
    onSuccess: (next) => client.setQueryData(TASK_KEY, next),
  })
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
                key={JSON.stringify(task.data.schedule_settings)}
                task={task.data}
                pending={change.isPending ? change.variables.runner : null}
                error={change.error}
                onChange={(runner, installed, schedule) => change.mutate({ runner, installed, schedule })}
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
  error: Error | null
  onChange: (runner: Runner, installed: boolean, schedule?: ScheduleSettings) => void
}

function TaskTile({ task, pending, error, onChange }: TaskTileProps) {
  const [schedule, setSchedule] = useState<ScheduleSettings>(
    task.schedule_settings ?? {
      cadence: "daily",
      time: "06:00",
      day: "MO",
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    },
  )
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [scheduleRunner, setScheduleRunner] = useState<Runner>("codex")
  const installed = task.installs.includes("codex")
  const waiting = task.codex_install_status === "pending"
  const busy = pending !== null || waiting
  return (
    <article className="flex flex-col gap-3 rounded-[var(--radius-l)] border border-line bg-card p-4">
      <div>
        <h2 className="m-0 text-sm font-semibold">{task.name}</h2>
        <p className="mb-0 mt-1 text-xs leading-relaxed text-muted-foreground">
          Keep Gmail, iMessage and WhatsApp up to date.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {(["codex", "claude"] satisfies Runner[]).map((runner) => {
          const on = task.installs.includes(runner)
          const working = pending === runner || (runner === "codex" && waiting)
          return (
            <Button
              key={runner}
              size="sm"
              shape="pill"
              variant={on ? "ok" : "default"}
              disabled={working}
              className="group"
              data-runner={runner}
              data-installed={on}
              title={runner === "claude" ? "Restarts Claude Desktop to pick up the change" : undefined}
              onClick={() => {
                if (on) onChange(runner, false)
                else {
                  setScheduleRunner(runner)
                  setScheduleOpen(true)
                }
              }}
            >
              <RunnerIcon runner={runner} />
              {working ? (
                on ? (
                  "Removing…"
                ) : (
                  <span>
                    Installing
                    <WaitingDots />
                  </span>
                )
              ) : on ? (
                <>
                  <span className="group-hover:hidden">{RUNNER_TITLE[runner]} ✓</span>
                  <span className="hidden group-hover:inline">Remove</span>
                </>
              ) : (
                `Install ${RUNNER_TITLE[runner]}`
              )}
            </Button>
          )
        })}
      </div>
      <ScheduleDialog
        open={scheduleOpen}
        onOpenChange={setScheduleOpen}
        title={
          task.installs.includes(scheduleRunner) ? "Edit schedule" : `Install ${RUNNER_TITLE[scheduleRunner]}`
        }
        submitLabel={task.installs.includes(scheduleRunner) ? "Save" : "Create"}
        replacesCustom={installed && !task.schedule_settings}
        schedule={schedule}
        onSchedule={setSchedule}
        busy={busy}
        onSubmit={() => {
          onChange(scheduleRunner, true, schedule)
          setScheduleOpen(false)
        }}
      />
      {task.codex_thread_url && (
        <div className="flex items-center gap-3 text-xs">
          <a className="font-semibold text-info no-underline hover:underline" href={task.codex_thread_url}>
            Open chat
          </a>
          {installed && (
            <button
              className="cursor-pointer border-0 bg-transparent p-0 text-muted-foreground hover:text-foreground"
              onClick={() => {
                setScheduleRunner("codex")
                setScheduleOpen(true)
              }}
            >
              Edit schedule
            </button>
          )}
        </div>
      )}
      {waiting && (
        <p role="status" className="m-0 text-xs text-muted-foreground">
          Waiting for Codex to register the schedule
          <WaitingDots />
        </p>
      )}
      {installed && !waiting && <p className="m-0 text-xs text-ok">{task.schedule}</p>}
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {errorText(error)}
        </p>
      )}
    </article>
  )
}

function WaitingDots() {
  return (
    <span aria-hidden="true" className="inline-flex">
      {[0, 150, 300].map((delay) => (
        <span key={delay} className="motion-safe:animate-pulse" style={{ animationDelay: `${delay}ms` }}>
          .
        </span>
      ))}
    </span>
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
