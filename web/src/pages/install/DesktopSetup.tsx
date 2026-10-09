import { useCallback, useEffect, useRef, useState, type FormEvent } from "react"

import { useNavigate } from "react-router-dom"

import { ImportIcon, SparkIcon } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import {
  continueSetup,
  focusApp,
  importData,
  importSource,
  messagesReadable,
  openExternal,
  type SetupAnswer,
} from "@/lib/api/install"
import { HOME } from "@/lib/nav"
import { cn } from "@/lib/utils"
import { openSignIn, POWERSET_CALLBACK } from "@/lib/signin"
import { isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

// LinkedIn's sign-in lands here; the app then reads the list (desktop/src-tauri/src/linkedin.rs).
const LINKEDIN_CONNECTIONS = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
// The waits setup stops at until the user resumes it; the others finish on their own.
const STOPPED = new Set(["error", "resume", "recovery", "details"])

// While setup waits for Full Disk Access, ask macOS again this often and continue by itself.
const PERMISSION_POLL_MS = 3_000

const INPUT =
  "min-h-9 w-full rounded-[var(--radius-s)] border border-line-strong bg-card px-3 text-[13px] text-foreground outline-none placeholder:text-faint focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"

/** The first dollar figure in a spend estimate, when it carries one. */
function cost(estimate: unknown): string | null {
  if (!isRecord(estimate)) return null
  const dollars = Object.entries(estimate).find(
    ([key, value]) => key.endsWith("usd") && typeof value === "number",
  )?.[1]
  return typeof dollars === "number" ? `About $${dollars.toFixed(2)}` : null
}

const CHOICE =
  "group flex w-full cursor-pointer flex-col items-start gap-2.5 rounded-[var(--radius-m)] border border-line-strong bg-card p-4 text-left text-foreground transition-[border-color,background-color,transform] duration-fast ease-out hover:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))] hover:bg-surface-2 active:translate-y-px disabled:cursor-default disabled:opacity-50 disabled:hover:border-line-strong disabled:hover:bg-card disabled:active:translate-y-0"

/** A home folder path with the home part as a tilde, the way people write it. */
function tilde(path: string): string {
  return path.replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/, "~")
}

/** The first click: set up from scratch, or import the command-line install's data. */
function Choice({
  disabled,
  onStart,
  onError,
}: {
  disabled: boolean
  onStart: (run: () => Promise<void>) => void
  onError: (message: string) => void
}) {
  const navigate = useNavigate()
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
    onStart(async () => {
      if (await importData()) await navigate(HOME.href)
    })
  return (
    <div className="grid w-full max-w-[560px] grid-cols-2 gap-3 max-[720px]:grid-cols-1">
      <button
        type="button"
        className={CHOICE}
        disabled={disabled}
        onClick={() => onStart(() => continueSetup({}))}
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
        className={cn(CHOICE, source && "border-[color-mix(in_srgb,var(--primary)_35%,var(--line-strong))]")}
        disabled={disabled || !source}
        onClick={importIt}
        data-import-source={source ?? ""}
      >
        <span className="grid size-9 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground group-hover:text-primary">
          <ImportIcon className="size-[18px]" />
        </span>
        <span className="text-[13.5px] font-bold">Import existing data</span>
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
  const provider = action?.kind === "signin" ? (action.provider ?? "powerset") : null
  const resume = () => void continueSetup({}).catch((caught: unknown) => setError(errorText(caught)))

  // Powerset and LinkedIn sign in inside the app; Google opens in the browser (its rule), and
  // the app comes back forward once setup moves on.
  const signIn = (url: string) => {
    if (provider === "google") {
      openExternal(url).catch((caught: unknown) => setError(errorText(caught)))
    } else if (provider === "linkedin") {
      openSignIn({ title: "LinkedIn", url, finish: LINKEDIN_CONNECTIONS, then: "linkedin", onDone: resume })
    } else {
      openSignIn({ title: "Powerset", url, finish: POWERSET_CALLBACK })
    }
  }
  useEffect(() => {
    if (signInUrl !== null) signIn(signInUrl)
    // The sign-in opens once per URL setup hands over.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- W5
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
  const wasGoogle = useRef(false)
  useEffect(() => {
    if (provider === "google") wasGoogle.current = true
    else if (wasGoogle.current) {
      wasGoogle.current = false
      void focusApp()
    }
  }, [provider])

  const run = useCallback((task: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    task()
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setBusy(false))
  }, [])
  const answer = (value: SetupAnswer) => run(() => continueSetup(value))

  let control
  if (signInUrl !== null) {
    const url = signInUrl
    control = (
      <Button variant={provider === "google" ? "default" : "primary"} onClick={() => signIn(url)}>
        {provider === "google" ? "Open Google again" : "Sign in"}
      </Button>
    )
  } else if (action?.kind === "approval" && action.step) {
    const step = action.step
    const price = cost(action.estimate)
    control = (
      <div className="flex flex-col items-center gap-2">
        {price && <p className="m-0 text-sm tabular-nums text-foreground">{price}</p>}
        <Button variant="primary" disabled={busy} onClick={() => answer({ approve: step })}>
          Approve and continue
        </Button>
      </div>
    )
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
  } else if (data.event === "setup.ready") {
    control = <Choice disabled={busy} onStart={run} onError={setError} />
  } else if (data.status === "failed" || (data.status === "waiting" && STOPPED.has(action?.kind ?? ""))) {
    control = (
      <Button variant="primary" disabled={busy} onClick={() => answer({})}>
        {data.status === "failed" ? "Try again" : "Continue setup"}
      </Button>
    )
  } else {
    return null
  }

  return (
    <section className="install-action flex flex-col items-center gap-2">
      {control}
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </section>
  )
}
