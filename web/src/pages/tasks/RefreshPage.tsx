import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router-dom"
import { Check, ChevronRight, TriangleAlert, X } from "lucide-react"

import { EmptyState, Page, Select, Spinner } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { fetchAccounts, reconnectGmail } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { REFRESH_KEY, refreshNow, saveRefreshSettings } from "@/lib/api/refresh"
import { cn } from "@/lib/utils"
import type {
  Problem,
  RefreshFrequency,
  RefreshRun,
  RefreshSettingsChange,
  RefreshState,
  SourceRun,
} from "@/types/refresh"

import {
  added,
  clock,
  lower,
  ordinal,
  people,
  SOURCE_TITLE,
  stamp,
  took,
  trigger,
  WEEKDAYS,
  when,
} from "./format"
import { useRefresh } from "./useRefresh"

const STATUS: Readonly<
  Record<RefreshRun["status"], { label: string; variant: "ok" | "warn" | "bad" | "info" }>
> = {
  running: { label: "Running", variant: "info" },
  ok: { label: "Synced", variant: "ok" },
  needs_you: { label: "Needs you", variant: "warn" },
  failed: { label: "Failed", variant: "bad" },
}

// A synced source is green with a check, a skipped one (signed out) yellow with a warning, a failed
// one red with an X.
const SOURCE_STATUS: Readonly<
  Record<SourceRun["status"], { label: string; variant: "ok" | "warn" | "bad" }>
> = {
  synced: { label: "Synced", variant: "ok" },
  skipped: { label: "Skipped: not signed in", variant: "warn" },
  failed: { label: "Failed", variant: "bad" },
}

function StatusIcon({ variant }: { variant: "ok" | "warn" | "bad" | "info" }) {
  if (variant === "ok") return <Check aria-hidden className="size-3" strokeWidth={3} />
  if (variant === "warn") return <TriangleAlert aria-hidden className="size-3" strokeWidth={2.5} />
  if (variant === "bad") return <X aria-hidden className="size-3" strokeWidth={3} />
  return <Spinner className="size-3" />
}

// Every hour of the day, as the time picker offers them.
const HOURS = Array.from({ length: 24 }, (_, hour) => `${String(hour).padStart(2, "0")}:00`)
const FREQUENCIES: readonly { value: RefreshFrequency; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
]
const WEEKDAY_OPTIONS = WEEKDAYS.map((name, index) => ({ value: String(index), label: name }))
const DAY_OPTIONS = Array.from({ length: 31 }, (_, index) => ({
  value: String(index + 1),
  label: ordinal(index + 1),
}))
const PICKER = "h-7 text-xs"

/** The refresh's schedule: on/off, how often, the day and time, Refresh now, then every run. */
export function RefreshPage() {
  const client = useQueryClient()
  const refresh = useRefresh()
  // The run whose details are open; undefined until one is clicked (the latest opens by itself
  // when it needs you).
  const [openId, setOpenId] = useState<number | null | undefined>(undefined)
  const settings = useMutation({
    mutationFn: saveRefreshSettings,
    onSuccess: (next) => client.setQueryData(REFRESH_KEY, next),
  })
  // The switch and the pickers move on the click; the server's answer replaces the guess, and a
  // refusal puts them back.
  const change = (next: RefreshSettingsChange) => {
    const previous = client.getQueryData<RefreshState>(REFRESH_KEY)
    if (previous)
      client.setQueryData<RefreshState>(REFRESH_KEY, {
        ...previous,
        settings: { ...previous.settings, ...next },
      })
    settings.mutate(next, { onError: () => client.setQueryData(REFRESH_KEY, previous) })
  }
  const start = useMutation({
    mutationFn: refreshNow,
    onSuccess: (next) => client.setQueryData(REFRESH_KEY, next),
  })
  const data = refresh.data
  const runs = data?.runs ?? []
  const latest = runs[0]
  const open =
    openId === undefined
      ? latest && latest.status !== "ok" && latest.status !== "running"
        ? latest.id
        : null
      : openId

  return (
    <Page title="Scheduled tasks">
      {refresh.error && !data && <EmptyState>{errorText(refresh.error)}</EmptyState>}
      {refresh.isPending && <EmptyState>Loading the refresh…</EmptyState>}
      {data && (
        <>
          <section
            aria-label="Schedule"
            className="flex flex-col gap-3 rounded-[var(--radius-l)] border border-line bg-card p-4"
          >
            <div className="flex items-center gap-2.5">
              <button
                type="button"
                role="switch"
                aria-checked={data.settings.enabled}
                aria-label="Refresh automatically"
                onClick={() => change({ enabled: !data.settings.enabled })}
                className={cn(
                  "relative h-5 w-[34px] shrink-0 cursor-pointer rounded-full border-0 p-0 transition-colors duration-fast ease-out",
                  data.settings.enabled ? "bg-ok" : "bg-line-strong",
                )}
              >
                <span
                  className={cn(
                    "absolute top-0.5 left-0.5 size-4 rounded-full bg-white transition-transform duration-fast ease-out",
                    data.settings.enabled && "translate-x-[14px]",
                  )}
                />
              </button>
              <h2 className="m-0 text-sm font-semibold">Refresh automatically</h2>
              <Button
                variant="primary"
                shape="pill"
                className="ml-auto"
                disabled={data.running !== null || start.isPending}
                onClick={() => start.mutate()}
              >
                {data.running || start.isPending ? <Spinner className="size-3.5" /> : null}
                {data.running ? "Refreshing…" : "Refresh now"}
              </Button>
            </div>
            <div
              className={cn(
                "flex flex-wrap items-center gap-2 text-xs",
                !data.settings.enabled && "text-muted-foreground",
              )}
            >
              <Select
                aria-label="Repeat"
                value={data.settings.frequency}
                onChange={(value) => {
                  const frequency = FREQUENCIES.find((option) => option.value === value)?.value
                  if (frequency) change({ frequency })
                }}
                options={FREQUENCIES}
                className={cn(PICKER, "min-w-[92px]")}
              />
              {data.settings.frequency === "weekly" && (
                <>
                  <span>on</span>
                  <Select
                    aria-label="Day of the week"
                    value={String(data.settings.weekday)}
                    onChange={(weekday) => change({ weekday: Number(weekday) })}
                    options={WEEKDAY_OPTIONS}
                    className={cn(PICKER, "min-w-[108px]")}
                  />
                </>
              )}
              {data.settings.frequency === "monthly" && (
                <>
                  <span>on the</span>
                  <Select
                    aria-label="Day of the month"
                    title={data.settings.day > 28 ? "A shorter month runs on its last day." : undefined}
                    value={String(data.settings.day)}
                    onChange={(day) => change({ day: Number(day) })}
                    options={DAY_OPTIONS}
                    className={cn(PICKER, "min-w-[68px]")}
                  />
                </>
              )}
              <span>at</span>
              <Select
                aria-label="Time of day"
                value={data.settings.time}
                onChange={(time) => change({ time })}
                options={(HOURS.includes(data.settings.time) ? HOURS : [data.settings.time, ...HOURS]).map(
                  (time) => ({ value: time, label: clock(time) }),
                )}
                className={cn(PICKER, "min-w-[96px]")}
              />
              <span className="ml-1 text-faint">
                {data.running ? (
                  <Link
                    to="/accounts?view=sync"
                    className="font-semibold text-info no-underline hover:underline"
                  >
                    See progress
                  </Link>
                ) : data.next_run_at ? (
                  `Next run ${lower(when(data.next_run_at))} · while the app is open`
                ) : (
                  "Off"
                )}
              </span>
            </div>
            {(settings.error ?? start.error) && (
              <p role="alert" className="m-0 text-xs text-bad">
                {errorText(settings.error ?? start.error)}
              </p>
            )}
          </section>
          <GmailReconnect />
          <History runs={runs} open={open} onToggle={(id) => setOpenId(id === open ? null : id)} />
        </>
      )}
    </Page>
  )
}

interface HistoryProps {
  runs: RefreshRun[]
  open: number | null
  onToggle: (id: number) => void
}

const CELL = "border-b border-line px-2.5 py-2.5"

function History({ runs, open, onToggle }: HistoryProps) {
  return (
    <section aria-labelledby="history" className="flex flex-col gap-2">
      <h2 id="history" className="m-0 text-[11px] font-semibold uppercase tracking-[.1em] text-faint">
        Runs
      </h2>
      {runs.length === 0 ? (
        <p className="m-0 text-xs text-muted-foreground">No runs yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs" aria-label="Runs">
            <thead>
              <tr className="text-left text-[11px] text-faint">
                {["When", "Status", "Sources", "People"].map((name) => (
                  <th key={name} className="border-b border-line px-2.5 pb-2 font-semibold whitespace-nowrap">
                    {name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <RunRow key={run.id} run={run} open={run.id === open} onToggle={() => onToggle(run.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function RunRow({ run, open, onToggle }: { run: RefreshRun; open: boolean; onToggle: () => void }) {
  return (
    <>
      <tr
        aria-selected={open}
        onClick={onToggle}
        className="cursor-pointer whitespace-nowrap hover:bg-card aria-selected:bg-surface-2"
      >
        <td className={CELL}>
          <button
            type="button"
            aria-expanded={open}
            onClick={(event) => {
              event.stopPropagation()
              onToggle()
            }}
            className="flex cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 text-left text-xs text-foreground"
          >
            <ChevronRight
              aria-hidden
              className={cn("size-3 text-faint transition-transform duration-fast", open && "rotate-90")}
            />
            {when(run.started_at)}
            <span className="text-faint">· {trigger(run)}</span>
          </button>
        </td>
        <td className={CELL}>
          <Badge variant={STATUS[run.status].variant}>
            <StatusIcon variant={STATUS[run.status].variant} />
            {STATUS[run.status].label}
          </Badge>
        </td>
        <td className={CELL}>
          <span className="flex items-center gap-1.5">
            {run.sources.map((source) =>
              source.status === "synced" ? (
                <span
                  key={source.source}
                  title={`${SOURCE_TITLE[source.source]}: ${SOURCE_STATUS.synced.label}`}
                  data-source-status="synced"
                  className="text-muted-foreground"
                >
                  {SOURCE_TITLE[source.source]}
                </span>
              ) : (
                <Badge
                  key={source.source}
                  variant={SOURCE_STATUS[source.status].variant}
                  title={`${SOURCE_TITLE[source.source]}: ${SOURCE_STATUS[source.status].label}`}
                  data-source-status={source.status}
                >
                  <StatusIcon variant={SOURCE_STATUS[source.status].variant} />
                  {SOURCE_TITLE[source.source]}
                </Badge>
              ),
            )}
          </span>
        </td>
        <td className={cn(CELL, "tabular-nums")}>{added(run)}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={4} className="border-b border-line bg-card p-0">
            <RunDetail run={run} />
          </td>
        </tr>
      )}
    </>
  )
}

function RunDetail({ run }: { run: RefreshRun }) {
  const rows: [string, string][] = [
    ["Started", stamp(run.started_at)],
    ["Took", run.finished_at ? took(run) : "Still running"],
    ...run.sources.map((source): [string, string] => [
      SOURCE_TITLE[source.source],
      `${SOURCE_STATUS[source.status].label}${
        source.accounts.length ? ` · ${source.accounts.join(", ")}` : ""
      }`,
    ]),
    ["People", people(run)],
    ["Search", run.index_rebuilt ? "Rebuilt" : "No change"],
  ]
  if (run.message) rows.push(["Last step", run.message])
  return (
    <article aria-label="Run details" className="grid grid-cols-2 gap-4 px-8 py-3 max-[820px]:grid-cols-1">
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 self-start text-xs">
        {rows.map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="text-faint">{name}</dt>
            <dd className="m-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-col gap-2">
        {run.problems.length === 0 ? (
          <p className="m-0 text-xs text-muted-foreground">
            {run.status === "running" ? "Running…" : "Nothing needs you."}
          </p>
        ) : (
          run.problems.map((problem) => (
            <div key={problem.title} className="border-l-2 border-warn py-0.5 pl-2.5 text-xs">
              <b className="block">{problem.title}</b>
              {problem.detail} {problem.source === "gmail" ? null : <ProblemLink problem={problem} />}
            </div>
          ))
        )}
        {run.error && !run.problems.some((problem) => problem.detail === run.error) && (
          <p className="m-0 text-xs break-words text-bad">{run.error}</p>
        )}
      </div>
    </article>
  )
}

function ProblemLink({ problem }: { problem: Problem }) {
  return (
    <Link to={problem.href} className="font-semibold text-info no-underline hover:underline">
      {problem.action}
    </Link>
  )
}

// While a sign-in runs, re-read the mailboxes (and their jobs) this often.
const JOB_POLL_MS = 2_000

/** Gmail's one place to sign back in: each mailbox whose sign-in is expired now, read live (not from
 *  a run), with its own Reconnect. Shown only while one needs it, or one reconnected here has an
 *  answer to show. One sign-in at a time: the others wait while one runs. */
function GmailReconnect() {
  const client = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: ({ signal }) => fetchAccounts(signal),
    refetchInterval: (query) =>
      Object.values(query.state.data?.jobs ?? {}).some((job) => job.state === "running")
        ? JOB_POLL_MS
        : false,
    refetchIntervalInBackground: true,
  })
  if (!accounts.data) return null
  const { jobs } = accounts.data
  const running = Object.values(jobs).some((job) => job.state === "running")
  const expired = accounts.data.accounts.filter(
    (account) => account.source === "gmail" && (account.health === "error" || jobs[account.name]),
  )
  if (!expired.length) return null
  const reconnect = (email: string) => {
    setError(null)
    reconnectGmail(email)
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => void client.invalidateQueries({ queryKey: ["accounts"] }))
  }
  return (
    <section
      aria-label="Gmail sign-in"
      className="flex flex-col gap-2 rounded-[var(--radius-m)] border border-line border-l-2 border-l-warn bg-card p-4 text-xs"
    >
      <h2 className="m-0 text-sm font-semibold">Gmail sign-in expired</h2>
      {expired.map((account) => {
        const job = jobs[account.name]
        const signingIn = job?.state === "running"
        const fixed = account.health !== "error" && !signingIn
        return (
          <span
            key={account.name}
            data-mailbox={account.name}
            className="flex items-center justify-between gap-2"
          >
            <span className="flex min-w-0 items-center gap-1.5 truncate">
              {signingIn && <Spinner className="size-3" />}
              {account.name}
              {job && (signingIn || job.state === "failed") && (
                <span
                  className={cn("truncate", job.state === "failed" ? "text-bad" : "text-muted-foreground")}
                >
                  · {job.step}
                </span>
              )}
            </span>
            {fixed ? (
              <span className="text-ok">Signed in</span>
            ) : (
              !signingIn && (
                <Button size="sm" variant="bad" disabled={running} onClick={() => reconnect(account.name)}>
                  Reconnect
                </Button>
              )
            )}
          </span>
        )
      })}
      {error && (
        <span role="alert" className="text-bad">
          {error}
        </span>
      )}
    </section>
  )
}
