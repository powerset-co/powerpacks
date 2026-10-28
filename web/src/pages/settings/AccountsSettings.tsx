import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type ReactNode } from "react"
import { Link } from "react-router-dom"

import { EmptyState, CHANNEL_ICON, Spinner } from "@/components/shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchAccounts, reconnectGmail, syncAccount } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { CHANNELS } from "@/lib/channels"
import { ago, stamp } from "@/lib/copy"
import { isDesktop } from "@/lib/desktop"
import type { Account, AccountSource, AccountHealth, Job, JobsPayload } from "@/types/accounts"

import { Connect } from "@/pages/install/Connect"
import { ConnectionRow } from "@/components/shared/ConnectionRow"

const HEALTH: Readonly<Record<AccountHealth, { label: string; variant: "ok" | "warn" | "bad" | "muted" }>> = {
  ok: { label: "Healthy", variant: "ok" },
  warning: { label: "Needs a look", variant: "warn" },
  error: { label: "Action needed", variant: "bad" },
  off: { label: "Not connected", variant: "muted" },
}

// While a sync or sign-in runs, re-read the accounts (and their jobs) this often.
const JOB_POLL_MS = 2_000
const LINKEDIN_EXPORT_URL = "https://www.linkedin.com/mypreferences/d/download-my-data"

function busy(job: Job | undefined): boolean {
  return job?.state === "running"
}

/** A card's job: a Gmail account's by its address, iMessage's or WhatsApp's by source. */
function jobFor(account: Account, jobs: JobsPayload): Job | undefined {
  return jobs[account.source === "gmail" ? account.name : account.source]
}

/** The connected message accounts, read live from each source's store on every load. */
export function AccountsSettings() {
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

  const detail = (source: AccountSource): ReactNode => {
    if (accounts.isPending) return <Skeleton aria-label="Loading account details" className="h-16 w-full" />
    const data = accounts.data
    if (!data) return null
    const matching = data.accounts.filter((account) => account.source === source)
    return matching.length ? (
      <div className="flex flex-col gap-4">
        {matching.map((account) => (
          <AccountDetails
            key={account.name}
            account={account}
            job={jobFor(account, data.jobs)}
            onReconnect={() => start(reconnectGmail(account.name))}
            onSync={() => start(syncAccount(account))}
          />
        ))}
      </div>
    ) : (
      <p className="m-0 text-xs text-muted-foreground">No imported data yet.</p>
    )
  }
  const details = {
    gmail: detail("gmail"),
    imessage: detail("imessage"),
    whatsapp: detail("whatsapp"),
    linkedin: detail("linkedin"),
  }
  return (
    <section className="flex flex-col gap-3" aria-label="Accounts">
      {accounts.error && <EmptyState>{errorText(accounts.error)}</EmptyState>}
      {actionError && (
        <p role="alert" className="m-0 text-xs text-bad">
          {actionError}
        </p>
      )}
      {isDesktop() ? (
        <Connect details={details} />
      ) : (
        <ul
          className="m-0 flex list-none flex-col divide-y divide-line overflow-hidden rounded-[var(--radius-m)] border border-line-strong bg-card p-0"
          aria-label="Accounts"
        >
          {accounts.isPending ? (
            <li className="p-4">
              <Skeleton aria-label="Loading accounts" className="h-16 w-full" />
            </li>
          ) : null}
          {Array.from(new Set(accounts.data?.accounts.map((account) => account.source))).map((source) => {
            const sourceAccounts =
              accounts.data?.accounts.filter((account) => account.source === source) ?? []
            const Icon = CHANNEL_ICON[source]
            return (
              <ConnectionRow
                key={source}
                title={CHANNELS[source].title}
                icon={<Icon className="size-[18px]" />}
                why="Sync status and imported data"
                state={sourceAccounts.some((account) => account.health !== "off") ? "ready" : "needed"}
                details={details[source]}
              />
            )
          })}
        </ul>
      )}
      {accounts.isPending ? <Skeleton aria-label="Loading sync schedule" className="h-4 w-48" /> : null}
      {accounts.data && <ScheduleLine scheduled={accounts.data.scheduled} />}
    </section>
  )
}

function ScheduleLine({ scheduled }: { scheduled: boolean }) {
  return (
    <p className="m-0 text-xs text-muted-foreground">
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

interface AccountDetailsProps {
  account: Account
  job: Job | undefined
  onReconnect: () => void
  onSync: () => void
}

function AccountDetails({ account, job, onReconnect, onSync }: AccountDetailsProps) {
  const health = HEALTH[account.health]
  const running = busy(job)
  const note = job && (running || job.state === "failed") ? job.step : account.note
  const tone = job?.state === "failed" ? "error" : running ? "ok" : account.health
  return (
    <div data-account={account.source}>
      <div className="flex items-center justify-between gap-3">
        <span className="text-[13px] font-semibold">{account.name}</span>
        <Badge variant={health.variant}>{health.label}</Badge>
      </div>
      <div className="mt-2 flex min-h-7 items-center justify-between gap-3">
        <span
          data-tone={tone}
          aria-live="polite"
          className="flex items-center gap-2 text-xs text-muted-foreground data-[tone=error]:text-bad data-[tone=warning]:text-warn"
        >
          {running && <Spinner />}
          {note}
        </span>
        {!running && <CardAction account={account} onReconnect={onReconnect} onSync={onSync} />}
      </div>
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
    </div>
  )
}

/** Reconnect for an expired Gmail sign-in; Sync for a signed-in source that has gone stale. */
function CardAction({ account, onReconnect, onSync }: Omit<AccountDetailsProps, "job">) {
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
