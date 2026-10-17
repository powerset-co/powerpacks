import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router-dom"

import { EmptyState, Page, SourcePill, Spinner } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchAccounts, reconnectGmail, syncAccount } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { ago, stamp } from "@/lib/copy"
import { isDesktop } from "@/lib/desktop"
import type { Account, AccountHealth, Job, JobsPayload } from "@/types/accounts"

import { CodexCard } from "./CodexCard"

const HEALTH: Readonly<Record<AccountHealth, { label: string; variant: "ok" | "warn" | "bad" | "muted" }>> = {
  ok: { label: "Healthy", variant: "ok" },
  warning: { label: "Needs a look", variant: "warn" },
  error: { label: "Action needed", variant: "bad" },
  off: { label: "Not connected", variant: "muted" },
}

// While a sync or sign-in runs, re-read the accounts (and their jobs) this often.
const JOB_POLL_MS = 2_000
const LINKEDIN_EXPORT_URL = "https://www.linkedin.com/mypreferences/d/download-my-data"

// Shared by the real schedule line and card and their loading shapes.
const SCHEDULE_BOX = "m-0 rounded-[var(--radius-m)] border border-line bg-card px-3.5 py-2.5 text-xs"
const CARD_BOX = "rounded-[var(--radius-l)] border border-line bg-card p-4 shadow-[var(--shadow-1)]"

function busy(job: Job | undefined): boolean {
  return job?.state === "running"
}

/** A card's job: a Gmail account's by its address, iMessage's or WhatsApp's by source. */
function jobFor(account: Account, jobs: JobsPayload): Job | undefined {
  return jobs[account.source === "gmail" ? account.name : account.source]
}

/** The connected message accounts, read live from each source's store on every load. */
export function AccountsPage() {
  const client = useQueryClient()
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: ({ signal }) => fetchAccounts(signal),
    refetchInterval: (query) =>
      Object.values(query.state.data?.jobs ?? {}).some(busy) ? JOB_POLL_MS : false,
    // A sync finishes whether or not this tab is showing; keep asking so it lands.
    refetchIntervalInBackground: true,
  })

  const [actionError, setActionError] = useState<string | null>(null)
  const start = (action: Promise<void>) => {
    setActionError(null)
    action
      .catch((error: unknown) => setActionError(errorText(error)))
      .finally(() => void client.invalidateQueries({ queryKey: ["accounts"] }))
  }

  return (
    <Page title="Accounts">
      {accounts.error && <EmptyState>{errorText(accounts.error)}</EmptyState>}
      {accounts.isPending && <ScheduleLineSkeleton />}
      {accounts.data && <ScheduleLine scheduled={accounts.data.scheduled} />}
      {actionError && <p className="m-0 text-xs text-bad">{actionError}</p>}
      <ul
        className="m-0 flex list-none flex-col gap-3 p-0"
        aria-label="Accounts"
        aria-busy={accounts.isPending}
      >
        {isDesktop() && <CodexCard />}
        {accounts.isPending && [0, 1].map((card) => <AccountCardSkeleton key={card} />)}
        {accounts.data?.accounts.map((account) => (
          <AccountCard
            key={`${account.source}:${account.name}`}
            account={account}
            job={jobFor(account, accounts.data.jobs)}
            onReconnect={() => start(reconnectGmail(account.name))}
            onSync={() => start(syncAccount(account))}
          />
        ))}
      </ul>
    </Page>
  )
}

// The schedule line's and an account card's shapes while the accounts first load, so the
// Codex card above them never moves and the real cards rise in where these stood.
function ScheduleLineSkeleton() {
  return (
    <div className={SCHEDULE_BOX}>
      <Skeleton className="h-4 w-48 bg-surface-2" />
      <span className="sr-only">Loading accounts…</span>
    </div>
  )
}

function AccountCardSkeleton() {
  return (
    <li aria-hidden className={CARD_BOX}>
      <div className="flex items-center gap-2.5">
        <Skeleton className="h-6 w-20 rounded-full bg-surface-2" />
        <Skeleton className="h-4 w-40 bg-surface-2" />
        <Skeleton className="ml-auto h-5 w-16 rounded-full bg-surface-2" />
      </div>
      <div className="mt-2 flex min-h-7 items-center">
        <Skeleton className="h-3 w-56 max-w-full bg-surface-2" />
      </div>
    </li>
  )
}

function ScheduleLine({ scheduled }: { scheduled: boolean }) {
  return (
    <p className={`${SCHEDULE_BOX} text-muted-foreground`}>
      {scheduled ? (
        "Refreshes daily at 6:00 AM."
      ) : (
        <>
          Daily refresh is off.{" "}
          <Link to="/tasks" className="font-semibold text-info no-underline hover:underline">
            Set it up
          </Link>
        </>
      )}
    </p>
  )
}

interface AccountCardProps {
  account: Account
  job: Job | undefined
  onReconnect: () => void
  onSync: () => void
}

function AccountCard({ account, job, onReconnect, onSync }: AccountCardProps) {
  const health = HEALTH[account.health]
  const running = busy(job)
  const note = job && (running || job.state === "failed") ? job.step : account.note
  const tone = job?.state === "failed" ? "error" : running ? "ok" : account.health
  return (
    <li
      data-account={account.source}
      data-health={account.health}
      className={`rise-in ${CARD_BOX} data-[health=error]:border-[color-mix(in_srgb,var(--bad)_45%,transparent)] data-[health=warning]:border-[color-mix(in_srgb,var(--warn)_40%,transparent)]`}
    >
      {/* The stats open on a click on the card and fold on the next. */}
      <details>
        <summary className="cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          <span className="flex items-center gap-2.5">
            <SourcePill channel={account.source} size="md" />
            <span className="min-w-0 truncate text-sm font-semibold">{account.name}</span>
            <Badge variant={health.variant} className="ml-auto">
              {health.label}
            </Badge>
          </span>
          <span className="mt-2 flex min-h-7 items-center justify-between gap-3">
            <span
              data-tone={tone}
              aria-live="polite"
              className="flex items-center gap-2 text-xs text-muted-foreground data-[tone=error]:text-bad data-[tone=warning]:text-warn"
            >
              {running && <Spinner />}
              {note}
            </span>
            {!running && <CardAction account={account} onReconnect={onReconnect} onSync={onSync} />}
          </span>
        </summary>
        <dl className="m-0 mt-3 grid grid-cols-4 gap-3 max-[680px]:grid-cols-2">
          {account.source === "linkedin" ? (
            <>
              <Stat label="Connections" value={account.contacts.toLocaleString()} />
              <Stat label="Export saved" value={stamp(account.last_sync_at)} />
            </>
          ) : (
            <>
              <Stat label="Messages" value={account.messages.toLocaleString()} />
              <Stat label="Contacts" value={account.contacts.toLocaleString()} />
              <Stat label="Newest message" value={ago(account.latest_message_at)} />
              <Stat label="Last sync" value={stamp(account.last_sync_at)} />
            </>
          )}
        </dl>
      </details>
    </li>
  )
}

/** Reconnect for an expired Gmail sign-in; Sync for a signed-in source that has gone stale. */
function CardAction({ account, onReconnect, onSync }: Omit<AccountCardProps, "job">) {
  if (account.source === "gmail" && account.health === "error") {
    return (
      <Button size="sm" variant="bad" onClick={onReconnect}>
        Reconnect
      </Button>
    )
  }
  if (account.health !== "warning") return null
  if (account.source === "linkedin") {
    return (
      <a
        href={LINKEDIN_EXPORT_URL}
        target="_blank"
        rel="noreferrer"
        className="text-xs font-semibold text-info no-underline hover:underline"
      >
        Download from LinkedIn
      </a>
    )
  }
  return (
    <Button size="sm" onClick={onSync}>
      Sync
    </Button>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">{label}</dt>
      <dd className="m-0 text-sm tabular-nums">{value}</dd>
    </div>
  )
}
