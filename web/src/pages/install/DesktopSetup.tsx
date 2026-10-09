import { useCallback, useEffect, useState, type FormEvent } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, messagesReadable, skipSource, type SetupAnswer } from "@/lib/api/install"
import { openSignIn, POWERSET_CALLBACK } from "@/lib/signin"
import type { InstallStatus } from "@/types/install"

// The waits setup stops at until the user resumes it; the others finish on their own.
const STOPPED = new Set(["error", "resume", "details", "gmail"])
// The WhatsApp steps: stopped at any of them, setup can go on without WhatsApp
// (packs/powerset/primitives/install/controller.py SKIPPABLE).
const WHATSAPP_STEPS = new Set(["whatsapp_tools", "whatsapp_login", "whatsapp_sync", "whatsapp_import"])

/** Powerset signs in inside the app; LinkedIn and Google sign in through Chrome, which brings
 *  the app back forward when it closes (packs/ingestion/primitives/common/browser.js returnFocus). */
function signInToPowerset(url: string): void {
  openSignIn({ title: "Powerset", url, finish: POWERSET_CALLBACK })
}

// While setup waits for Full Disk Access, ask macOS again this often and continue by itself.
const PERMISSION_POLL_MS = 3_000

const INPUT =
  "min-h-9 w-full rounded-[var(--radius-s)] border border-line-strong bg-card px-3 text-[13px] text-foreground outline-none placeholder:text-faint focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"

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
      key={`${data.step}-${action?.kind ?? ""}`}
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
