import { useEffect, useRef, useState, type FormEvent } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, focusApp, openExternal, type SetupAnswer } from "@/lib/api/install"
import { openSignIn } from "@/lib/signin"
import { isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

// Where the Powerset login's own callback server listens (packs/powerset/primitives/auth/auth.py).
const POWERSET_CALLBACK = "http://localhost:9876/callback"
// LinkedIn's sign-in lands here; the app then reads the list (desktop/src-tauri/src/linkedin.rs).
const LINKEDIN_CONNECTIONS = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
// The waits setup stops at until the user resumes it; the others finish on their own.
const STOPPED = new Set(["error", "resume", "recovery", "details"])

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
  const wasGoogle = useRef(false)
  useEffect(() => {
    if (provider === "google") wasGoogle.current = true
    else if (wasGoogle.current) {
      wasGoogle.current = false
      void focusApp()
    }
  }, [provider])

  const answer = (value: SetupAnswer) => {
    setBusy(true)
    setError(null)
    continueSetup(value)
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setBusy(false))
  }

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
