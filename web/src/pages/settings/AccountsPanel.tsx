import { useEffect, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useSearchParams } from "react-router-dom"
import { ChevronRight, TriangleAlert } from "lucide-react"
import { CheckIcon, Spinner } from "@/components/shared"
import type { ConnectionRowProps } from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import { fetchAccount } from "@/lib/api/account"
import { continueSetup, fetchConnection, type SetupAnswer } from "@/lib/api/install"
import { errorText } from "@/lib/api/http"
import { isDesktop } from "@/lib/desktop"
import { readStored, writeStored } from "@/lib/storage"
import { cn } from "@/lib/utils"
import { Connect, type ConnectRow } from "@/pages/install/Connect"
import { DesktopSetup } from "@/pages/install/DesktopSetup"
import { live, POLL_MS, STEPS } from "@/pages/install/sources"
import { Guide } from "@/pages/onboarding/guide/Guide"
import { Checklist } from "@/pages/onboarding/Checklist"
import { PageShot } from "@/pages/onboarding/PageShot"
import { linkedinSetup, useRun } from "@/pages/onboarding/run"
import type { AccountSource, AccountsPayload } from "@/types/accounts"

import { HowItWorks } from "./how-it-works/HowItWorks"
import { RefreshPrompt } from "./RefreshPrompt"

// Remembered on this Mac once How Deep Context works is closed; the "here" link opens it again.
const HOW_IT_WORKS_KEY = "accounts.howItWorks"
const SOURCES: readonly ConnectRow[] = ["imessage", "linkedin", "gmail", "whatsapp"]
const SERVICES: readonly ConnectRow[] = ["browser", "codex"]
const SUBTITLE: Record<ConnectRow, string> = {
  imessage: "iMessage & Contacts",
  linkedin: "Your connections",
  gmail: "Your email conversations",
  whatsapp: "Your chats",
  powerset: "Your Powerpacks account",
  codex: "Your ChatGPT plan",
  browser: "Your Chrome profile",
}

/** One connection controller, with its rows on the left and its actions and guides on the right. */
export function AccountsPanel({
  details,
  accounts,
  error,
}: {
  details: Record<AccountSource, ReactNode>
  accounts?: AccountsPayload
  error: string | null
}) {
  const [params, setParams] = useSearchParams()
  // A refresh problem links here with ?source=<row> to open that source.
  const [selected, setSelected] = useState<ConnectRow>(() => {
    const source = params.get("source")
    return SOURCES.find((key) => key === source) ?? "imessage"
  })
  const [startError, setStartError] = useState<string | null>(null)
  // How Deep Context works opens the first time Accounts does.
  const [howItWorks, setHowItWorks] = useState(
    () => readStored("local", HOW_IT_WORKS_KEY, (raw) => (raw === true ? true : null)) === null,
  )
  const openHowItWorks = (open: boolean) => {
    setHowItWorks(open)
    if (!open) writeStored("local", HOW_IT_WORKS_KEY, true)
  }
  const jobs = Object.values(accounts?.jobs ?? {})
  const syncing = jobs.some((job) => job.state === "running")
  const progress = params.get("view") === "sync"
  const client = useQueryClient()
  // The pane shows Enrich's run (bin/onboard, also the daily refresh's), never onboarding's
  // LinkedIn setup, and after a click only what that click's run wrote.
  const run = useRun((data) => !linkedinSetup(data))
  const starting = run.starting
  // Any run going, this pane's or not, holds Enrich.
  const active = run.latest !== undefined && live(run.latest)
  const account = useQuery({ queryKey: ["account"], queryFn: ({ signal }) => fetchAccount(signal) })
  const connection = useQuery({
    queryKey: ["install", "connect"],
    queryFn: ({ signal }) => fetchConnection(signal),
    refetchInterval: POLL_MS,
  })
  const gmailWorking =
    connection.data && live(connection.data) && STEPS.gmail.includes(connection.data.step)
      ? connection.data
      : undefined
  useEffect(() => {
    if (!run.data) return
    if (run.ready) void client.invalidateQueries({ queryKey: ["accounts"] })
  }, [run.data, run.ready, client])
  const start = (answer: SetupAnswer) => {
    setStartError(null)
    setParams({ view: "sync" }, { replace: true, state: { startedAt: Date.now() } })
    return continueSetup(answer).catch((caught: unknown) => {
      setStartError(errorText(caught))
      setParams({ view: "sync" }, { replace: true })
    })
  }
  const choose = (key: ConnectRow) => {
    setSelected(key)
    setParams({}, { replace: true })
  }
  const layout = (items: Readonly<Record<ConnectRow, ConnectionRowProps>>) => {
    const item = items[selected]
    const connected = item.state === "ready"
    const sourceGuide =
      selected === "imessage" || selected === "gmail" || selected === "whatsapp" ? selected : null
    // A source shows its guide until it is connected, never after.
    const showGuide = sourceGuide && !connected
    const connecting = Object.values(items).some((entry) => entry.state === "working")
    // A sign-in, a sync or Enrich under way holds every other account's Connect.
    const held = item.state === "needed" && (connecting || syncing || active || starting)
    // A Gmail mailbox whose sign-in is gone: the row says so; Scheduled tasks reconnects it.
    const expired = accounts?.accounts.some((entry) => entry.source === "gmail" && entry.health === "error")
    const list = (keys: readonly ConnectRow[]) => (
      <div className="flex flex-col gap-1">
        {keys.map((key) => {
          const row = items[key]
          const warn = key === "gmail" && row.state === "ready" && expired
          return (
            <button
              key={key}
              type="button"
              aria-pressed={selected === key && !progress}
              onClick={() => choose(key)}
              className={cn(
                "flex w-full cursor-pointer items-center gap-3 rounded-[var(--radius-m)] border border-transparent bg-transparent px-3 py-3.5 text-left hover:bg-card",
                selected === key && !progress && "border-line-strong bg-surface-2",
              )}
            >
              <span className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-surface-2">
                {row.icon}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold">{row.title}</span>
                <span className="mt-1 block text-[11px] text-muted-foreground">{SUBTITLE[key]}</span>
              </span>
              {/* One outline pill for every state; only its icon is coloured: a green check once
                  connected, a yellow alert while a Gmail mailbox needs signing in, a spinner while it works. */}
              <span
                data-status={warn ? "needs" : row.state}
                className={cn(
                  "flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px]",
                  row.state === "ready"
                    ? "border-line-strong text-foreground"
                    : "border-line text-muted-foreground",
                )}
              >
                {warn ? (
                  <TriangleAlert aria-hidden className="size-3 text-warn" />
                ) : row.state === "ready" ? (
                  <CheckIcon className="size-3 text-ok" />
                ) : row.state === "working" || row.state === "checking" ? (
                  <Spinner className="size-3" />
                ) : null}
                {warn
                  ? "Needs sign-in"
                  : row.state === "ready"
                    ? "Connected"
                    : row.state === "working"
                      ? "Working"
                      : row.state === "checking"
                        ? "Checking"
                        : "Not connected"}
              </span>
              <ChevronRight className="size-3 text-faint" />
            </button>
          )
        })}
      </div>
    )
    return (
      <div className="grid min-h-full flex-1 grid-cols-2 max-[900px]:grid-cols-1">
        <section className="px-8 py-10 max-[680px]:px-4">
          <div className="mx-auto flex w-full max-w-[410px] flex-col gap-6">
            <div>
              <h1 className="m-0 text-[24px] font-semibold tracking-tight">Accounts</h1>
              <p className="mb-0 mt-2 text-sm text-muted-foreground">
                Your people, in one place. See how your data is processed{" "}
                <button
                  type="button"
                  aria-label="See how your data is processed here"
                  className="cursor-pointer border-0 bg-transparent p-0 text-primary underline-offset-2 hover:underline"
                  onClick={() => setHowItWorks(true)}
                >
                  here
                </button>
                .
              </p>
            </div>
            <div>
              <h2 className="mb-3 px-3 text-[10px] font-semibold uppercase tracking-[.1em] text-faint">
                Your sources
              </h2>
              {list(SOURCES)}
            </div>
            <div>
              <h2 className="mb-3 px-3 text-[10px] font-semibold uppercase tracking-[.1em] text-faint">
                Account Profiles
              </h2>
              {list(SERVICES)}
            </div>
            <RefreshPrompt
              sources={SOURCES.map((key) => ({
                key,
                title: items[key].title,
                icon: items[key].icon,
                state: items[key].state,
              }))}
            />
            {error || startError ? (
              <p role="alert" className="m-0 px-3 text-xs text-bad">
                {error ?? startError}
              </p>
            ) : null}
          </div>
        </section>
        <aside className="border-l border-line bg-card px-8 py-10 max-[900px]:border-l-0 max-[900px]:border-t max-[680px]:px-4">
          <div className="mx-auto flex w-full max-w-[450px] flex-col gap-5">
            {progress ? (
              <>
                <p className="m-0 text-[10px] font-semibold uppercase tracking-[.1em] text-faint">
                  Sync & Deep Context
                </p>
                <Checklist run={run} controls={false} />
                {run.data ? <DesktopSetup data={run.data} onContinue={start} /> : null}
                <p className="text-xs text-muted-foreground">
                  You can keep using Powerpacks. Progress stays here and in the sidebar.
                </p>
              </>
            ) : (
              <>
                {showGuide ? (
                  <Guide
                    source={sourceGuide}
                    person={{ name: account.data?.name ?? "You", email: account.data?.email ?? "" }}
                  />
                ) : (
                  <div>
                    <p className="mb-3 mt-0 text-[10px] font-semibold uppercase tracking-[.1em] text-faint">
                      {SOURCES.includes(selected) ? "Your sources" : "Your account"}
                    </p>
                    <h2 className="m-0 text-[23px] font-semibold tracking-tight">{item.title}</h2>
                  </div>
                )}
                <div className="flex flex-col gap-3 text-xs text-muted-foreground">
                  {/* A source's one-line why repeats its guide or its subtitle; Gmail's is its address
                      and Add a mailbox, and the account profiles keep theirs. */}
                  {!SOURCES.includes(selected) || selected === "gmail" ? item.why : null}
                  {item.children}
                </div>
                {gmailWorking && selected === "gmail" ? (
                  <>
                    <p className="m-0 flex items-center gap-2 text-xs">
                      <Spinner />
                      {gmailWorking.message}
                    </p>
                    <PageShot
                      url="/api/install/gmail-page"
                      address="console.cloud.google.com"
                      caption="Connecting Gmail"
                      reading
                    />
                  </>
                ) : null}
                {item.state === "needed" && item.action ? (
                  <Button
                    variant="primary"
                    className="self-start"
                    disabled={(item.action.disabled ?? false) || !isDesktop() || active || starting}
                    onClick={item.action.onClick}
                  >
                    {selected === "imessage" ? (
                      "Open System Settings"
                    ) : (
                      <>
                        {item.action.label} {item.title}
                      </>
                    )}
                  </Button>
                ) : null}
                {held ? (
                  <p className="m-0 text-xs text-muted-foreground">
                    {connecting
                      ? "Another account is connecting. This one can connect once it finishes."
                      : "Wait for the sync to finish to connect this one."}
                  </p>
                ) : null}
                {connected ? item.details : null}
                {!connected && selected === "linkedin" ? (
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    Sign in to LinkedIn in the window that opens. The next sync reads your connections.
                  </p>
                ) : null}
              </>
            )}
          </div>
        </aside>
      </div>
    )
  }
  return (
    <>
      <Connect disabled={active || starting || syncing} details={details} layout={layout} />
      <HowItWorks open={howItWorks} onOpenChange={openHowItWorks} />
    </>
  )
}
