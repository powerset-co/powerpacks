import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useState, type ReactNode } from "react"

import { useNavigate } from "react-router-dom"

import {
  BrowserIcon,
  CHANNEL_ICON,
  CheckIcon,
  CloudIcon,
  ImportIcon,
  SparkIcon,
  Spinner,
} from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import {
  continueSetup,
  fetchPreflight,
  importData,
  importSource,
  installAction,
  installPreflight,
  messagesReadable,
  type PreflightInstall,
} from "@/lib/api/install"
import { HOME } from "@/lib/nav"
import { cn } from "@/lib/utils"

const MessagesIcon = CHANNEL_ICON.imessage

// While a check waits on macOS or an install, ask again this often.
const POLL_MS = 2_000

const CHOICE =
  "group flex w-full cursor-pointer flex-col items-start gap-2.5 rounded-[var(--radius-m)] border border-line-strong bg-card p-4 text-left text-foreground transition-[border-color,background-color,transform,opacity] duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] hover:bg-surface-2 active:translate-y-px disabled:cursor-default disabled:hover:border-line-strong disabled:hover:bg-card disabled:active:translate-y-0"

/** One setup prerequisite, with a labeled action when it needs attention. */
function Check({
  icon,
  title,
  why,
  state,
  fix,
  note,
}: {
  icon: ReactNode
  title: string
  why: string
  state: "checking" | "working" | "ready" | "needed"
  fix?: { label: string; onClick: () => void }
  note?: ReactNode
}) {
  const mark = "rise-in grid size-7 shrink-0 place-items-center self-center rounded-full"
  return (
    <li className="flex items-start gap-3 px-4 py-3.5">
      <span className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground">
        {icon}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13.5px] font-bold">{title}</span>
        <span className="text-xs leading-snug text-muted-foreground">{why}</span>
        {note}
      </div>
      {state === "needed" && fix ? (
        <Button key={state} size="sm" onClick={fix.onClick} className="shrink-0 self-center">
          {fix.label}
        </Button>
      ) : (
        <span
          key={state}
          role="img"
          aria-label={state === "ready" ? "Ready" : "Working"}
          className={cn(mark, state === "ready" ? "bg-ok-soft text-ok" : "text-faint")}
        >
          {state === "ready" ? <CheckIcon className="size-3.5" /> : <Spinner />}
        </span>
      )}
    </li>
  )
}

/** A background install's live line, or why it failed. */
function InstallLine({ install }: { install: PreflightInstall | null }) {
  if (install?.status === "failed") {
    return <span className="text-xs text-bad">{install.message}</span>
  }
  if (install?.status === "running" && install.line) {
    return <span className="truncate text-[11.5px] text-faint">{install.line}</span>
  }
  return null
}

/** Whether macOS lets Powerpacks read Messages, asked again while not yet; null until the first answer. */
function useMessagesReadable(): boolean | null {
  const [readable, setReadable] = useState<boolean | null>(null)
  useEffect(() => {
    let stopped = false
    const check = async () => {
      const allowed = await messagesReadable()
      if (stopped) return
      setReadable(allowed)
      if (allowed) stopped = true
    }
    const timer = setInterval(() => void check(), POLL_MS)
    void check()
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [])
  return readable
}

/** A preflight item's icon: checking until the first answer, then ready, installing, or needed. */
function status(item: { ok: boolean; install: PreflightInstall | null } | undefined) {
  if (!item) return "checking" as const
  if (item.ok) return "ready" as const
  return item.install?.status === "running" ? ("working" as const) : ("needed" as const)
}

/** What a new setup needs from this Mac. Turning on
 *  Full Disk Access makes macOS quit and reopen the app, harmless here and a hang mid-setup. */
function PreflightCheck({ onBack }: { onBack: () => void }) {
  const readable = useMessagesReadable()
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { data } = useQuery({
    queryKey: ["install", "preflight"],
    queryFn: ({ signal }) => fetchPreflight(signal),
    refetchInterval: (query) =>
      query.state.data?.browser.install?.status === "running" ||
      query.state.data?.gcloud.install?.status === "running"
        ? 1_000
        : POLL_MS,
  })
  const install = (item: "chromium" | "gcloud") =>
    void installPreflight(item).catch((caught: unknown) => setError(errorText(caught)))
  // Full Disk Access is optional: without it setup leaves iMessage out.
  const ready = data?.browser.ok === true && data.gcloud.ok
  const start = () => {
    setStarting(true)
    setError(null)
    void continueSetup({}).catch((caught: unknown) => {
      setError(errorText(caught))
      setStarting(false)
    })
  }
  return (
    <div className="flex w-full flex-col gap-4">
      <ul className="m-0 flex list-none flex-col divide-y divide-line rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left">
        <Check
          icon={<MessagesIcon className="size-[18px]" />}
          title="Full Disk Access"
          why="Optional. Read iMessage and Contacts to find the people you talk to."
          state={readable === null ? "checking" : readable ? "ready" : "needed"}
          fix={{
            label: "Open Settings",
            onClick: () =>
              void installAction("permissions").catch((caught: unknown) => setError(errorText(caught))),
          }}
        />
        <Check
          icon={<BrowserIcon className="size-[18px]" />}
          title="A browser for sign-ins"
          why="Opens LinkedIn and Google sign-in pages. The browser download is about 150 MB."
          state={status(data?.browser)}
          fix={{ label: "Install browser", onClick: () => install("chromium") }}
          note={data && !data.browser.ok ? <InstallLine install={data.browser.install} /> : null}
        />
        <Check
          icon={<CloudIcon className="size-[18px]" />}
          title="Google tools"
          why="Prepares Google access so Powerpacks can connect Gmail."
          state={status(data?.gcloud)}
          fix={{ label: "Install Google tools", onClick: () => install("gcloud") }}
          note={data && !data.gcloud.ok ? <InstallLine install={data.gcloud.install} /> : null}
        />
      </ul>
      <div className="flex items-center justify-between gap-3">
        <Button disabled={starting} onClick={onBack}>
          Back
        </Button>
        <Button variant="primary" disabled={!ready || starting} onClick={start}>
          {starting ? "Starting setup…" : "Continue"}
        </Button>
      </div>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  )
}

/** A home folder path with the home part as a tilde, the way people write it. */
function tilde(path: string): string {
  return path.replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, "~")
}

/** Import existing data directly; only a new setup needs the prerequisite check. */
function Choice({ onStart }: { onStart: () => void }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [clicked, setClicked] = useState<"import" | null>(null)
  const [error, setError] = useState<string | null>(null)
  const chosen = error ? null : clicked
  // undefined while the app looks; null when there is nothing to import.
  const [source, setSource] = useState<string | null | undefined>(undefined)
  const fail = useCallback((caught: unknown) => setError(errorText(caught)), [])
  useEffect(() => {
    importSource()
      .then(setSource)
      .catch((caught: unknown) => {
        setSource(null)
        fail(caught)
      })
  }, [fail])
  const choose = (task: () => Promise<void>) => {
    setError(null)
    setClicked("import")
    task().catch(fail)
  }
  return (
    <div className="flex w-full flex-col gap-4">
      <div className="grid w-full grid-cols-2 gap-3 max-[720px]:grid-cols-1">
        <button
          type="button"
          className={cn(CHOICE, chosen === "import" && "opacity-40")}
          disabled={chosen !== null}
          onClick={onStart}
        >
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-primary-soft text-primary">
            <SparkIcon className="size-[18px]" />
          </span>
          <span className="text-[13.5px] font-bold">Start setup</span>
          <span className="text-xs leading-snug text-muted-foreground">
            Sign in, import your contacts and build your network from scratch.
          </span>
        </button>
        <button
          type="button"
          className={cn(
            CHOICE,
            source && "border-[color-mix(in_srgb,var(--primary)_35%,var(--line-strong))]",
            !source && "opacity-40",
          )}
          disabled={chosen !== null || !source}
          onClick={() =>
            choose(async () => {
              const done = await importData()
              await client.invalidateQueries()
              if (done) await navigate(HOME.href)
            })
          }
          data-import-source={source ?? ""}
        >
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground group-hover:text-primary">
            {chosen === "import" ? <Spinner className="size-4" /> : <ImportIcon className="size-[18px]" />}
          </span>
          <span className="text-[13.5px] font-bold">
            {chosen === "import" ? "Importing…" : "Import existing data"}
          </span>
          <span className="text-xs leading-snug text-muted-foreground">
            {source === undefined
              ? "Looking for a Powerpacks install on this computer…"
              : source === null
                ? "No command-line Powerpacks install found on this computer."
                : `Use the network, imports and account already in ${tilde(source)}.`}
          </span>
        </button>
      </div>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  )
}

/** The desktop app's setup before it starts: import, or check prerequisites for a new setup. The
 *  third screen, the installation status, is the install page itself once setup runs. */
export function DesktopWelcome() {
  const [stage, setStage] = useState<0 | 1>(0)
  return (
    <div className="flex w-full flex-col items-center gap-6">
      <div key={stage} className="rise-in flex w-full flex-col items-center gap-5">
        <header className="flex flex-col items-center gap-1.5 text-center">
          <h2 className="m-0 text-xl font-bold">Setup</h2>
          <p className="m-0 text-sm text-muted-foreground">
            {stage === 0
              ? "Start fresh, or bring over what you already have on this Mac."
              : "A few things Powerpacks needs on this Mac before it starts."}
          </p>
        </header>
        {stage === 0 ? <Choice onStart={() => setStage(1)} /> : <PreflightCheck onBack={() => setStage(0)} />}
      </div>
    </div>
  )
}
