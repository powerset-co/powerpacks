import { useQuery } from "@tanstack/react-query"
import { type ReactNode } from "react"
import { Link } from "react-router-dom"
import { TriangleAlert } from "lucide-react"

import { EmptyState, CHANNEL_ICON, Spinner } from "@/components/shared"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchAccounts } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { CHANNELS } from "@/lib/channels"
import { ago, stamp } from "@/lib/copy"
import { isDesktop } from "@/lib/desktop"
import type { Account, AccountSource, Job, JobsPayload } from "@/types/accounts"

import { AccountsPanel } from "./AccountsPanel"
import { Connect } from "@/pages/install/Connect"
import { ConnectionRow } from "@/components/shared/ConnectionRow"

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
export function AccountsSettings({ panel = false }: { panel?: boolean }) {
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: ({ signal }) => fetchAccounts(signal),
    refetchInterval: (query) =>
      Object.values(query.state.data?.jobs ?? {}).some(busy) ? JOB_POLL_MS : false,
    // A sync finishes whether or not this tab is showing; keep asking so it lands.
    refetchIntervalInBackground: true,
  })

  const detail = (source: AccountSource): ReactNode => {
    if (accounts.isPending) return <Skeleton aria-label="Loading account details" className="h-16 w-full" />
    const data = accounts.data
    if (!data) return null
    const matching = data.accounts.filter((account) => account.source === source)
    return matching.length ? (
      <div className="flex flex-col gap-4">
        {matching.map((account) => (
          <AccountDetails key={account.name} account={account} job={jobFor(account, data.jobs)} />
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
  if (panel)
    return (
      <AccountsPanel
        details={details}
        accounts={accounts.data}
        error={accounts.error ? errorText(accounts.error) : null}
      />
    )
  return (
    <section className="flex flex-col gap-3" aria-label="Accounts">
      {accounts.error && <EmptyState>{errorText(accounts.error)}</EmptyState>}
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

function ScheduleLine({ scheduled }: { scheduled: string | null }) {
  return (
    <p className="m-0 text-xs text-muted-foreground">
      {scheduled ? (
        `Syncs ${scheduled}.`
      ) : (
        <>
          Automatic sync is off.{" "}
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
}

function AccountDetails({ account, job }: AccountDetailsProps) {
  const running = busy(job)
  const note = job && (running || job.state === "failed") ? job.step : account.note
  const tone = job?.state === "failed" ? "error" : running ? "ok" : account.health
  return (
    <div data-account={account.source}>
      {/* Gmail can keep several mailboxes, so each card names its own; the other sources have one. */}
      {account.source === "gmail" ? <span className="text-[13px] font-semibold">{account.name}</span> : null}
      <div className="mt-1 flex min-h-7 items-center justify-between gap-3">
        <span
          data-tone={tone}
          aria-live="polite"
          className="flex items-center gap-2 text-xs text-muted-foreground"
        >
          {running && <Spinner />}
          {!running && (tone === "error" || tone === "warning") && (
            <TriangleAlert aria-hidden className="size-3 shrink-0" />
          )}
          {note}
        </span>
        {!running && <CardAction account={account} />}
      </div>
      <dl className="m-0 mt-3 grid grid-cols-2 gap-4">
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

/** What a card asks of the person: an expired Gmail sign-in points to Scheduled tasks, which
 *  reconnects it; a stale LinkedIn export to LinkedIn's download. Syncing is Scheduled tasks'
 *  (Refresh now and the daily run), so no card syncs on its own. */
function CardAction({ account }: { account: Account }) {
  if (account.source === "gmail" && account.health === "error")
    return (
      <Link to="/tasks" className="text-xs text-primary no-underline hover:underline">
        Reconnect in Scheduled tasks
      </Link>
    )
  if (account.source !== "linkedin" || account.health !== "warning") return null
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

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] font-semibold uppercase tracking-[.08em] text-faint">{label}</dt>
      <dd className="m-0 text-sm tabular-nums">{value}</dd>
    </div>
  )
}
