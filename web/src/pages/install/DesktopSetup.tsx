import { useCallback, useEffect, useState, type FormEvent } from "react"

import { useNavigate } from "react-router-dom"

import { CHANNEL_ICON, ImportIcon, SparkIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import {
  continueSetup,
  importData,
  importSource,
  installAction,
  messagesReadable,
  skipSource,
  type SetupAnswer,
} from "@/lib/api/install"
import { HOME } from "@/lib/nav"
import { cn } from "@/lib/utils"
import { openSignIn, POWERSET_CALLBACK } from "@/lib/signin"
import type { InstallStatus } from "@/types/install"

// The waits setup stops at until the user resumes it; the others finish on their own.
const STOPPED = new Set(["error", "resume", "details", "gmail"])
// The WhatsApp steps: stopped at any of them, setup can go on without WhatsApp
// (packs/powerset/primitives/install/controller.py SKIPPABLE).
const WHATSAPP_STEPS = new Set(["whatsapp_tools", "whatsapp_login", "whatsapp_sync", "whatsapp_import"])

const MessagesIcon = CHANNEL_ICON.imessage

/** Powerset signs in inside the app; LinkedIn and Google sign in through Chrome, and setup
 *  brings the app forward after (workflow.py _back_to_app). */
function signInToPowerset(url: string): void {
  openSignIn({ title: "Powerset", url, finish: POWERSET_CALLBACK })
}

// While setup waits for Full Disk Access, ask macOS again this often and continue by itself.
const PERMISSION_POLL_MS = 3_000

const INPUT =
  "min-h-9 w-full rounded-[var(--radius-s)] border border-line-strong bg-card px-3 text-[13px] text-foreground outline-none placeholder:text-faint focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"

const CHOICE =
  "group flex w-full cursor-pointer flex-col items-start gap-2.5 rounded-[var(--radius-m)] border border-line-strong bg-card p-4 text-left text-foreground transition-[border-color,background-color,transform,opacity] duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] hover:bg-surface-2 active:translate-y-px disabled:cursor-default disabled:hover:border-line-strong disabled:hover:bg-card disabled:active:translate-y-0"

/** A home folder path with the home part as a tilde, the way people write it. */
function tilde(path: string): string {
  return path.replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, "~")
}

/** The first click: set up from scratch, or import the command-line install's data. */
function Choice({
  failed,
  onStart,
  onError,
}: {
  failed: boolean
  onStart: (run: () => Promise<void>) => void
  onError: (message: string) => void
}) {
  const navigate = useNavigate()
  // The clicked card spins until setup moves past the welcome (this unmounts) or the click fails.
  const [clicked, setClicked] = useState<"start" | "import" | null>(null)
  const chosen = failed ? null : clicked
  const choose = (which: "start" | "import", task: () => Promise<void>) => {
    setClicked(which)
    onStart(task)
  }
  // undefined while the app looks; null when there is nothing to import.
  const [source, setSource] = useState<string | null | undefined>(undefined)
  useEffect(() => {
    importSource()
      .then(setSource)
      .catch((caught: unknown) => {
        setSource(null)
        onError(errorText(caught))
      })
  }, [onError])
  const importIt = () =>
    choose("import", async () => {
      if (await importData()) await navigate(HOME.href)
    })
  return (
    <div className="grid w-full max-w-[560px] grid-cols-2 gap-3 max-[720px]:grid-cols-1">
      <button
        type="button"
        className={cn(CHOICE, chosen === "import" && "opacity-40")}
        disabled={chosen !== null}
        data-chosen={chosen === "start"}
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
        data-chosen={chosen === "import"}
        onClick={importIt}
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
  )
}

/** Whether macOS lets Powerpacks read Messages, asked again every few seconds while shown;
 *  null until the first answer. */
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
    const timer = setInterval(() => void check(), PERMISSION_POLL_MS)
    void check()
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [])
  return readable
}

/** The first screen: the one macOS permission setup needs, asked for before setup starts. Turning
 *  it on makes macOS quit and reopen the app, which is harmless now and hangs a running setup. */
function Permissions({ onDone }: { onDone: () => void }) {
  const readable = useMessagesReadable()
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (readable) onDone()
  }, [readable, onDone])
  return (
    <div className="flex w-full max-w-[560px] flex-col gap-3">
      <div className="flex items-start gap-3 rounded-[var(--radius-m)] border border-line-strong bg-card p-4 text-left">
        <span className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-surface-2">
          <MessagesIcon className="size-[18px]" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-[13.5px] font-bold">Allow Full Disk Access</span>
          <span className="text-xs leading-snug text-muted-foreground">
            Powerpacks reads iMessage and Contacts on this Mac to find who you talk to. Turn it on in the
            list; macOS reopens the app and you land back here.
          </span>
        </div>
        <Button
          variant="primary"
          disabled={readable === null}
          onClick={() =>
            void installAction("permissions").catch((caught: unknown) => setError(errorText(caught)))
          }
        >
          Open Settings
        </Button>
      </div>
      <div className="flex items-center justify-between gap-3 px-1">
        <span className="text-xs text-faint">
          {readable === null ? "Checking…" : "Waiting for Full Disk Access…"}
        </span>
        <button
          type="button"
          onClick={onDone}
          className="cursor-pointer border-0 bg-transparent p-0 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          Skip, no iMessage
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

/** One text field and a submit button, for the waits that need a value. */
function Ask({
  label,
  placeholder,
  type,
  onAnswer,
}: {
  label: string
  placeholder: string
  type: "email" | "url"
  onAnswer: (value: string) => void
}) {
  const [value, setValue] = useState("")
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (value.trim()) onAnswer(value.trim())
  }
  return (
    <form onSubmit={submit} className="mx-auto flex w-full max-w-[420px] flex-col gap-2 text-left">
      <label className="text-xs font-semibold text-muted-foreground" htmlFor="setup-answer">
        {label}
      </label>
      <div className="flex gap-2">
        <input
          id="setup-answer"
          type={type}
          value={value}
          placeholder={placeholder}
          onChange={(event) => setValue(event.target.value)}
          className={INPUT}
        />
        <Button type="submit" variant="primary" disabled={!value.trim()}>
          Continue
        </Button>
      </div>
    </form>
  )
}

/** In the desktop app nobody runs setup's commands in chat: its waits get buttons here, and
 *  the Powerset sign-in shows in the app's sign-in pane. */
export function DesktopSetup({ data }: { data: InstallStatus }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const action = data.action
  const signInUrl = action?.kind === "signin" ? (action.url ?? null) : null
  const resume = () => void continueSetup({}).catch((caught: unknown) => setError(errorText(caught)))

  // The sign-in opens once per URL setup hands over.
  useEffect(() => {
    if (signInUrl !== null) signInToPowerset(signInUrl)
  }, [signInUrl])
  // Full Disk Access: setup resumes on its own the moment macOS reports the grant.
  const waitingForMessages = action?.kind === "permission"
  useEffect(() => {
    if (!waitingForMessages) return
    let stopped = false
    const check = async () => {
      if (stopped || !(await messagesReadable())) return
      stopped = true
      resume()
    }
    const timer = setInterval(() => void check(), PERMISSION_POLL_MS)
    void check()
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [waitingForMessages])

  const run = useCallback((task: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    task()
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setBusy(false))
  }, [])
  const answer = (value: SetupAnswer) => run(() => continueSetup(value))
  const [permissionsDone, setPermissionsDone] = useState(false)
  const finishPermissions = useCallback(() => setPermissionsDone(true), [])
  // Stopped at a WhatsApp step (its QR, a failed link): go on without it. The server saves the
  // skip and stops the waiting run; setup then resumes from the saved choices.
  const skipWhatsapp = () =>
    run(async () => {
      await skipSource("whatsapp")
      await continueSetup({})
    })
  const stopped = data.status === "failed" || (data.status === "waiting" && STOPPED.has(action?.kind ?? ""))
  const skip =
    WHATSAPP_STEPS.has(data.step) && (action?.kind === "qr" || stopped) ? (
      <Button variant="default" disabled={busy} onClick={skipWhatsapp}>
        {data.prose.page["qr.skip"]}
      </Button>
    ) : null

  let control
  if (signInUrl !== null) {
    const url = signInUrl
    control = (
      <Button variant="primary" onClick={() => signInToPowerset(url)}>
        Sign in
      </Button>
    )
  } else if (action?.kind === "qr") {
    control = skip
  } else if (
    action?.kind === "gmail" &&
    data.step === "gmail_login" &&
    data.event === "gmail.which_accounts"
  ) {
    control = (
      <Ask
        label="Gmail account"
        placeholder="you@gmail.com"
        type="email"
        onAnswer={(email) => answer({ gmailEmail: email })}
      />
    )
  } else if (action?.kind === "owner") {
    control = (
      <Ask
        label="Your LinkedIn profile"
        placeholder="https://www.linkedin.com/in/your-name"
        type="url"
        onAnswer={(url) => answer({ linkedinUrl: url })}
      />
    )
  } else if (action?.kind === "permission") {
    // The page's own button opens System Settings; after the grant, setup restarts here.
    control = (
      <Button variant="primary" disabled={busy} onClick={() => answer({})}>
        I turned it on
      </Button>
    )
  } else if (data.event === "setup.ready" && !permissionsDone) {
    control = <Permissions onDone={finishPermissions} />
  } else if (data.event === "setup.ready") {
    control = <Choice failed={error !== null} onStart={run} onError={setError} />
  } else if (stopped) {
    control = (
      <div className="flex flex-wrap items-center justify-center gap-2">
        <Button variant="primary" disabled={busy} onClick={() => answer({})}>
          {data.status === "failed" ? "Try again" : "Continue setup"}
        </Button>
        {skip}
      </div>
    )
  } else {
    return null
  }

  return (
    // Each new wait rises in rather than swapping in place.
    <section
      key={`${permissionsDone}-${data.event === "setup.ready"}-${data.step}-${action?.kind ?? ""}`}
      className="install-action rise-in flex flex-col items-center gap-2"
    >
      {control}
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </section>
  )
}
