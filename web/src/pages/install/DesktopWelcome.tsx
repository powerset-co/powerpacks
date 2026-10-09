import { useQuery } from "@tanstack/react-query"
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

/** One thing this Mac needs: what it is, why, and either a check or the button that gets it. */
function Check({
  icon,
  title,
  why,
  ready,
  children,
  note,
}: {
  icon: ReactNode
  title: string
  why: string
  /** The words beside the check once it is ready; null while it is not. */
  ready: string | null
  /** The action while it is not ready. */
  children?: ReactNode
  note?: ReactNode
}) {
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
      <div className="flex shrink-0 items-center gap-2 self-center">
        {ready !== null ? (
          <span className="rise-in flex items-center gap-1.5 text-xs font-semibold text-ok">
            <CheckIcon className="size-3.5" />
            {ready}
          </span>
        ) : (
          children
        )}
      </div>
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

function InstallButton({
  install,
  label,
  onClick,
}: {
  install: PreflightInstall | null
  label: string
  onClick: () => void
}) {
  const running = install?.status === "running"
  return (
    <Button variant="primary" disabled={running} onClick={onClick}>
      {running && <Spinner />}
      {running ? "Installing…" : install?.status === "failed" ? "Try again" : label}
    </Button>
  )
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

/** The first screen: everything setup needs from this Mac, asked for before it starts. Turning on
 *  Full Disk Access makes macOS quit and reopen the app, harmless here and a hang mid-setup. */
function PreflightCheck({ onDone }: { onDone: () => void }) {
  const readable = useMessagesReadable()
  const [skipMessages, setSkipMessages] = useState(false)
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
  const messagesOk = readable === true || skipMessages
  const ready = messagesOk && data?.browser.ok === true && data.gcloud.ok
  return (
    <div className="flex w-full flex-col gap-4">
      <ul className="m-0 flex list-none flex-col divide-y divide-line rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left">
        <Check
          icon={<MessagesIcon className="size-[18px]" />}
          title="Full Disk Access"
          why="Reads iMessage and Contacts to find who you talk to. Nothing leaves your computer."
          ready={readable ? "Allowed" : skipMessages ? "Skipped" : null}
          note={
            !readable && !skipMessages ? (
              <button
                type="button"
                onClick={() => setSkipMessages(true)}
                className="w-fit cursor-pointer border-0 bg-transparent p-0 text-[11.5px] text-faint underline-offset-2 hover:text-foreground hover:underline"
              >
                Skip, no iMessage
              </button>
            ) : null
          }
        >
          <Button
            variant="primary"
            disabled={readable === null}
            onClick={() =>
              void installAction("permissions").catch((caught: unknown) => setError(errorText(caught)))
            }
          >
            {readable === null && <Spinner />}
            Open Settings
          </Button>
        </Check>
        <Check
          icon={<BrowserIcon className="size-[18px]" />}
          title="A browser for sign-ins"
          why="Signs in to LinkedIn and Google in a browser window. Nothing leaves your computer."
          ready={data?.browser.ok ? `Using ${data.browser.name}` : null}
          note={data && !data.browser.ok ? <InstallLine install={data.browser.install} /> : null}
        >
          {data ? (
            <InstallButton
              install={data.browser.install}
              label="Install Chromium"
              onClick={() => install("chromium")}
            />
          ) : (
            <Spinner />
          )}
        </Check>
        <Check
          icon={<CloudIcon className="size-[18px]" />}
          title="Google Cloud CLI"
          why="Connects Gmail through a private app in your own Google Cloud. Nothing leaves your computer."
          ready={data?.gcloud.ok ? "Installed" : null}
          note={data && !data.gcloud.ok ? <InstallLine install={data.gcloud.install} /> : null}
        >
          {data ? (
            <InstallButton install={data.gcloud.install} label="Install" onClick={() => install("gcloud")} />
          ) : (
            <Spinner />
          )}
        </Check>
      </ul>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-faint">
          {ready ? "This Mac is ready." : "Everything here runs on this Mac."}
        </span>
        <Button variant="primary" disabled={!ready} onClick={onDone}>
          Continue
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

/** The second screen: set up from scratch, or import the command-line install's data. The clicked
 *  card spins until setup moves on (this unmounts) or the click fails. */
function Choice({ onBack }: { onBack: () => void }) {
  const navigate = useNavigate()
  const [clicked, setClicked] = useState<"start" | "import" | null>(null)
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
  const choose = (which: "start" | "import", task: () => Promise<void>) => {
    setError(null)
    setClicked(which)
    task().catch(fail)
  }
  return (
    <div className="flex w-full flex-col gap-4">
      <div className="grid w-full grid-cols-2 gap-3 max-[720px]:grid-cols-1">
        <button
          type="button"
          className={cn(CHOICE, chosen === "import" && "opacity-40")}
          disabled={chosen !== null}
          onClick={() => choose("start", () => continueSetup({}))}
        >
          <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-primary-soft text-primary">
            {chosen === "start" ? <Spinner className="size-4" /> : <SparkIcon className="size-[18px]" />}
          </span>
          <span className="text-[13.5px] font-bold">
            {chosen === "start" ? "Starting setup…" : "Start setup"}
          </span>
          <span className="text-xs leading-snug text-muted-foreground">
            Sign in, import your contacts and build your network from scratch.
          </span>
        </button>
        <button
          type="button"
          className={cn(
            CHOICE,
            source && "border-[color-mix(in_srgb,var(--primary)_35%,var(--line-strong))]",
            (chosen === "start" || !source) && "opacity-40",
          )}
          disabled={chosen !== null || !source}
          onClick={() =>
            choose("import", async () => {
              if (await importData()) await navigate(HOME.href)
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
      <button
        type="button"
        onClick={onBack}
        disabled={chosen !== null}
        className="w-fit cursor-pointer border-0 bg-transparent p-0 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline disabled:opacity-40"
      >
        ← Preflight check
      </button>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  )
}

/** The desktop app's setup before it starts: the preflight check, then install or import. The
 *  third screen, the installation status, is the install page itself once setup runs. */
export function DesktopWelcome() {
  const [stage, setStage] = useState<0 | 1>(0)
  return (
    <div className="flex w-full flex-col items-center gap-6">
      <div key={stage} className="rise-in flex w-full flex-col items-center gap-5">
        <header className="flex flex-col items-center gap-1.5 text-center">
          <h2 className="m-0 text-xl font-bold">{stage === 0 ? "Preflight check" : "Set up Powerpacks"}</h2>
          <p className="m-0 text-sm text-muted-foreground">
            {stage === 0
              ? "A few things Powerpacks needs on this Mac before it starts."
              : "Start fresh, or bring over what you already have on this Mac."}
          </p>
        </header>
        {stage === 0 ? <PreflightCheck onDone={() => setStage(1)} /> : <Choice onBack={() => setStage(0)} />}
      </div>
    </div>
  )
}
