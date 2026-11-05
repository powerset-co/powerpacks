import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router-dom"
import { Check, TriangleAlert, X } from "lucide-react"

import { EmptyState, Page, Select, Spinner } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { fetchAccounts, reconnectGmail } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { REFRESH_KEY, refreshNow, saveRefreshSettings } from "@/lib/api/refresh"
import { cn } from "@/lib/utils"
import type { Problem, RefreshRun, RefreshState, SourceRun } from "@/types/refresh"

import { added, clock, lower, people, SOURCE_TITLE, stamp, took, trigger, when, lastLine } from "./format"
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

/** The native daily refresh: on/off and its time, Refresh now, then every run it left behind. */
export function RefreshPage() {
  const client = useQueryClient()
  const refresh = useRefresh()
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const settings = useMutation({
    mutationFn: saveRefreshSettings,
    onSuccess: (next) => client.setQueryData(REFRESH_KEY, next),
  })
  // The switch and the time move on the click; the server's answer replaces the guess, and a
  // refusal puts them back.
  const change = (next: { enabled?: boolean; time?: string }) => {
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
  const selected = runs.find((run) => run.id === selectedId) ?? runs[0]

  return (
    <Page title="Refresh">
      {refresh.error && !data && <EmptyState>{errorText(refresh.error)}</EmptyState>}
      {refresh.isPending && <EmptyState>Loading the refresh…</EmptyState>}
      {data && (
        <>
          <section
            aria-label="Daily refresh"
            className="flex flex-wrap items-start gap-5 rounded-[var(--radius-l)] border border-line bg-card p-5"
          >
            <div className="min-w-[260px] flex-1">
              <h2 className="m-0 text-sm font-semibold">Keep your network up to date</h2>
              <p className="mb-0 mt-1 max-w-[540px] text-xs leading-relaxed text-muted-foreground">
                Syncs LinkedIn, Gmail, Messages and WhatsApp, runs Deep Context on what&apos;s new, and
                rebuilds search when there are new people. Runs while the app is open; a missed day folds into
                the next run.
              </p>
              <div className="mt-4 flex flex-wrap items-center gap-2.5 text-xs">
                <button
                  type="button"
                  role="switch"
                  aria-checked={data.settings.enabled}
                  aria-label="Refresh every day"
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
                <span>Every day at</span>
                <Select
                  aria-label="Time of day"
                  value={data.settings.time}
                  onChange={(time) => change({ time })}
                  options={(HOURS.includes(data.settings.time) ? HOURS : [data.settings.time, ...HOURS]).map(
                    (time) => ({ value: time, label: clock(time) }),
                  )}
                  className="h-7 min-w-[96px] text-xs"
                />
                <span className="text-faint">
                  {data.running
                    ? "Refreshing now"
                    : data.next_run_at
                      ? `Next run ${lower(when(data.next_run_at))}`
                      : "Off"}
                </span>
              </div>
              {settings.error && (
                <p role="alert" className="mb-0 mt-2 text-xs text-bad">
                  {errorText(settings.error)}
                </p>
              )}
            </div>
            <div className="ml-auto flex flex-col items-end gap-1.5">
              <Button
                variant="primary"
                shape="pill"
                disabled={data.running !== null || start.isPending}
                onClick={() => start.mutate()}
              >
                {data.running || start.isPending ? <Spinner className="size-3.5" /> : null}
                {data.running ? "Refreshing…" : "Refresh now"}
              </Button>
              {data.running ? (
                <Link
                  to="/accounts?view=sync"
                  className="text-[11px] font-semibold text-info no-underline hover:underline"
                >
                  See progress
                </Link>
              ) : runs[0] ? (
                <span className="text-[11px] text-faint">{lastLine(runs[0])}</span>
              ) : null}
              {start.error && (
                <p role="alert" className="m-0 max-w-[260px] text-right text-[11px] text-bad">
                  {errorText(start.error)}
                </p>
              )}
            </div>
          </section>
          <History runs={runs} selected={selected} onSelect={setSelectedId} />
        </>
      )}
    </Page>
  )
}

interface HistoryProps {
  runs: RefreshRun[]
  selected: RefreshRun | undefined
  onSelect: (id: number) => void
}

function History({ runs, selected, onSelect }: HistoryProps) {
  return (
    <section aria-labelledby="history" className="flex flex-col gap-3">
      <h2 id="history" className="m-0 text-[11px] font-semibold uppercase tracking-[.1em] text-faint">
        Run history
      </h2>
      {runs.length === 0 ? (
        <p className="m-0 text-xs text-muted-foreground">No runs yet.</p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs" aria-label="Runs">
              <thead>
                <tr className="text-left text-[11px] text-faint">
                  {["When", "Trigger", "Status", "Sources", "People added", "Search", "Took"].map((name) => (
                    <th
                      key={name}
                      className="border-b border-line px-2.5 pb-2 font-semibold whitespace-nowrap"
                    >
                      {name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.id}
                    aria-selected={run === selected}
                    onClick={() => onSelect(run.id)}
                    className="cursor-pointer whitespace-nowrap hover:bg-card aria-selected:bg-surface-2"
                  >
                    <td className="border-b border-line px-2.5 py-2.5">
                      <button
                        type="button"
                        onClick={() => onSelect(run.id)}
                        className="cursor-pointer border-0 bg-transparent p-0 text-left text-xs text-foreground"
                      >
                        {when(run.started_at)}
                      </button>
                    </td>
                    <td className="border-b border-line px-2.5 py-2.5 text-muted-foreground">
                      {trigger(run)}
                    </td>
                    <td className="border-b border-line px-2.5 py-2.5">
                      <Badge variant={STATUS[run.status].variant}>
                        <StatusIcon variant={STATUS[run.status].variant} />
                        {STATUS[run.status].label}
                      </Badge>
                    </td>
                    <td className="border-b border-line px-2.5 py-2.5">
                      <span className="flex gap-1">
                        {run.sources.map((source) => (
                          <Badge
                            key={source.source}
                            variant={SOURCE_STATUS[source.status].variant}
                            title={`${SOURCE_TITLE[source.source]}: ${SOURCE_STATUS[source.status].label}`}
                            data-source-status={source.status}
                          >
                            <StatusIcon variant={SOURCE_STATUS[source.status].variant} />
                            {SOURCE_TITLE[source.source]}
                            {source.accounts.length > 1 ? ` · ${source.accounts.length}` : ""}
                          </Badge>
                        ))}
                      </span>
                    </td>
                    <td className="border-b border-line px-2.5 py-2.5 tabular-nums">{added(run)}</td>
                    <td className="border-b border-line px-2.5 py-2.5 text-muted-foreground">
                      {run.index_rebuilt ? "Rebuilt" : run.people_after === null ? "—" : "No change"}
                    </td>
                    <td className="border-b border-line px-2.5 py-2.5 tabular-nums text-muted-foreground">
                      {took(run)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {selected && <RunDetail run={selected} />}
        </>
      )}
    </section>
  )
}

function RunDetail({ run }: { run: RefreshRun }) {
  const rows: [string, string][] = [
    ["Started", stamp(run.started_at)],
    ["Finished", run.finished_at ? stamp(run.finished_at) : "Still running"],
    ...run.sources.map((source): [string, string] => [
      SOURCE_TITLE[source.source],
      `${SOURCE_STATUS[source.status].label}${
        source.accounts.length ? ` · ${source.accounts.join(", ")}` : ""
      }`,
    ]),
    ["People", people(run)],
    ["Search", run.index_rebuilt ? "Rebuilt with the new people" : "No change"],
  ]
  if (run.missed_days)
    rows.push(["Caught up", `${run.missed_days} missed day${run.missed_days > 1 ? "s" : ""}`])
  if (run.message) rows.push(["Last step", run.message])
  return (
    <article
      aria-label="Run details"
      className="grid grid-cols-2 gap-4 rounded-[var(--radius-m)] border border-line bg-card p-4 max-[820px]:grid-cols-1"
    >
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
              {problem.detail}{" "}
              {problem.source === "gmail" ? <GmailReconnect /> : <ProblemLink problem={problem} />}
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

/** Reconnect, right here, each Gmail mailbox whose sign-in is expired now (read live, so one
 *  signed in since drops off). One sign-in at a time: the others wait while one runs. */
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
  if (!accounts.data) return accounts.isPending ? <Spinner className="inline-block size-3" /> : null
  const { jobs } = accounts.data
  const running = Object.values(jobs).some((job) => job.state === "running")
  const expired = accounts.data.accounts.filter(
    (account) => account.source === "gmail" && (account.health === "error" || jobs[account.name]),
  )
  if (!expired.length) return <span className="text-muted-foreground">Every mailbox is signed in now.</span>
  const reconnect = (email: string) => {
    setError(null)
    reconnectGmail(email)
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => void client.invalidateQueries({ queryKey: ["accounts"] }))
  }
  return (
    <span className="mt-1.5 flex flex-col gap-1.5">
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
    </span>
  )
}
