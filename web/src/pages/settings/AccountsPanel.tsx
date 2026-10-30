import { useEffect, useState, type ReactNode } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useSearchParams } from "react-router-dom"
import { ChevronRight, RefreshCw } from "lucide-react"
import { CheckIcon, Spinner } from "@/components/shared"
import type { ConnectionRowProps } from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import { fetchAccount } from "@/lib/api/account"
import { continueSetup, fetchConnection, type SetupAnswer } from "@/lib/api/install"
import { errorText } from "@/lib/api/http"
import { isDesktop } from "@/lib/desktop"
import { cn } from "@/lib/utils"
import { Connect, type ConnectRow } from "@/pages/install/Connect"
import { DesktopSetup } from "@/pages/install/DesktopSetup"
import { live, POLL_MS, STEPS } from "@/pages/install/sources"
import { Guide } from "@/pages/onboarding/guide/Guide"
import type { GuideSource } from "@/pages/onboarding/guide/demos"
import { Checklist } from "@/pages/onboarding/Checklist"
import { PageShot } from "@/pages/onboarding/PageShot"
import { useRun } from "@/pages/onboarding/run"
import type { InstallStatus } from "@/types/install"
import type { AccountSource, AccountsPayload } from "@/types/accounts"

const SOURCES: readonly ConnectRow[] = ["imessage", "linkedin", "gmail", "whatsapp"]
const SERVICES: readonly ConnectRow[] = ["powerset", "codex"]
const SUBTITLE: Record<ConnectRow, string> = {
  imessage: "iMessage & Contacts",
  linkedin: "Your connections",
  gmail: "Your email conversations",
  whatsapp: "Your chats",
  powerset: "Your Powerpacks account",
  codex: "Your ChatGPT plan",
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
  const [selected, setSelected] = useState<ConnectRow>("imessage")
  const [guide, setGuide] = useState<GuideSource | null>(null)
  const [previous, setPrevious] = useState<InstallStatus>()
  const [startError, setStartError] = useState<string | null>(null)
  const progress = params.get("view") === "sync"
  const client = useQueryClient()
  const run = useRun(() => true, previous)
  const starting = run.starting
  const active = !run.ready && run.data !== undefined && live(run.data)
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
    setPrevious(run.data)
    setStartError(null)
    setParams({ view: "sync" }, { replace: true })
    return continueSetup(answer).catch((caught: unknown) => {
      setStartError(errorText(caught))
      setPrevious(undefined)
    })
  }
  const choose = (key: ConnectRow) => {
    setSelected(key)
    setGuide(null)
    setParams({}, { replace: true })
  }
  const layout = (items: Readonly<Record<ConnectRow, ConnectionRowProps>>) => {
    const item = items[selected]
    const connected = item.state === "ready"
    const sourceGuide =
      selected === "imessage" || selected === "gmail" || selected === "whatsapp" ? selected : null
    const showGuide = sourceGuide && (!connected || guide === sourceGuide)
    const connecting = Object.values(items).some((entry) => entry.state === "working")
    const canSync = SOURCES.some((key) => items[key].state === "ready")
    const list = (keys: readonly ConnectRow[]) => (
      <div className="flex flex-col gap-1">
        {keys.map((key) => {
          const row = items[key]
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
              <span
                className={cn(
                  "flex shrink-0 items-center gap-1 text-[11px]",
                  row.state === "ready" ? "text-ok" : "text-muted-foreground",
                )}
              >
                {row.state === "ready" ? (
                  <CheckIcon className="size-3" />
                ) : row.state === "working" || row.state === "checking" ? (
                  <Spinner className="size-3" />
                ) : null}
                {row.state === "ready"
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
              <p className="mb-0 mt-2 text-sm text-muted-foreground">Your people, in one place.</p>
            </div>
            <div>
              <h2 className="mb-3 px-3 text-[10px] font-semibold uppercase tracking-[.1em] text-faint">
                Your sources
              </h2>
              {list(SOURCES)}
            </div>
            <div>
              <h2 className="mb-3 px-3 text-[10px] font-semibold uppercase tracking-[.1em] text-faint">
                Powerpacks & chat
              </h2>
              {list(SERVICES)}
            </div>
            <div className="mt-3 flex flex-col gap-3 px-3">
              <p className="m-0 text-xs text-muted-foreground">
                {active || starting
                  ? "Updating in the background."
                  : accounts?.scheduled
                    ? "Refreshes daily at 6:00 AM."
                    : "Sync when you're ready."}
              </p>
              <Button
                variant="primary"
                disabled={!isDesktop() || !run.data || !canSync || connecting || active || starting}
                onClick={() =>
                  void start({ sources: ["linkedin", "gmail", "imessage", "whatsapp"], refresh: true })
                }
              >
                {active || starting ? <Spinner /> : <RefreshCw className="size-3.5" />}
                {active || starting ? "Syncing…" : "Resync all"}
              </Button>
              <p className="m-0 text-center text-[11px] text-muted-foreground">
                Sync connected sources, then update Deep Context and search.
              </p>
              {run.data && run.data.event !== "setup.ready" ? (
                <button
                  type="button"
                  className="border-0 bg-transparent text-xs text-primary"
                  onClick={() => setParams({ view: "sync" }, { replace: true })}
                >
                  View sync progress
                </button>
              ) : null}
              {error || startError ? (
                <p role="alert" className="m-0 text-xs text-bad">
                  {error ?? startError}
                </p>
              ) : null}
            </div>
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
                  {item.why}
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
                {connected && !showGuide ? item.details : null}
                {connected && sourceGuide ? (
                  <button
                    type="button"
                    className="self-start border-0 bg-transparent p-0 text-xs text-primary"
                    onClick={() => setGuide(showGuide ? null : sourceGuide)}
                  >
                    {showGuide ? "Back to account" : "View connection guide"}
                  </button>
                ) : null}
                {!connected && selected === "linkedin" ? (
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    Sign in to LinkedIn in the window that opens. Then Resync all imports your connections
                    alongside your other sources.
                  </p>
                ) : null}
              </>
            )}
          </div>
        </aside>
      </div>
    )
  }
  return <Connect disabled={active || starting} details={details} onGuide={setGuide} layout={layout} />
}
