import { useCallback, useEffect, useRef, useState, type FormEvent } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, focusApp, openExternal, type SetupAnswer } from "@/lib/api/install"
import type { InstallStatus } from "@/types/install"

// The waits setup stops at until the user resumes it; the others finish on their own.
const STOPPED = new Set(["error", "resume", "details"])

const INPUT =
  "min-h-9 w-full rounded-[var(--radius-s)] border border-line-strong bg-card px-3 text-[13px] text-foreground outline-none placeholder:text-faint focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"

/** The LinkedIn profile setup's owner step asks for when it found no LinkedIn session. */
function ProfileForm({ busy, onAnswer }: { busy: boolean; onAnswer: (url: string) => void }) {
  const [value, setValue] = useState("")
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!busy && value.trim()) onAnswer(value.trim())
  }
  return (
    <form onSubmit={submit} className="mx-auto flex w-full max-w-[420px] flex-col gap-2 text-left">
      <label className="text-xs font-semibold text-muted-foreground" htmlFor="setup-answer">
        Your LinkedIn profile
      </label>
      <div className="flex gap-2">
        <input
          id="setup-answer"
          type="url"
          value={value}
          disabled={busy}
          placeholder="https://www.linkedin.com/in/your-name"
          onChange={(event) => setValue(event.target.value)}
          className={INPUT}
        />
        <Button type="submit" variant="primary" disabled={busy || !value.trim()}>
          {busy ? "Continuing…" : "Continue"}
        </Button>
      </div>
    </form>
  )
}

/** In the desktop app nobody runs setup's commands in chat. Its sign-ins happen on the Connect
 *  screen before it starts (Connect.tsx); what it can still ask for gets a button here: a
 *  Powerset sign-in gone stale (in the default browser, then the app comes back forward), the
 *  owner's LinkedIn profile, and a step that stopped. */
export function DesktopSetup({ data }: { data: InstallStatus }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const action = data.action
  const signInUrl = action?.kind === "signin" ? (action.url ?? null) : null

  // The sign-in opens once per URL setup hands over; once setup moves past it, the app comes
  // back forward from the browser.
  const signingIn = useRef(false)
  useEffect(() => {
    if (signInUrl !== null) void openExternal(signInUrl)
    else if (signingIn.current) void focusApp()
    signingIn.current = signInUrl !== null
  }, [signInUrl])

  const run = useCallback((task: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    task()
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setBusy(false))
  }, [])
  const answer = (value: SetupAnswer) => run(() => continueSetup(value))
  const stopped =
    data.status === "failed" ||
    (data.status === "waiting" && !data.installer_pid && STOPPED.has(action?.kind ?? ""))

  let control
  if (signInUrl !== null) {
    const url = signInUrl
    control = (
      <Button variant="primary" onClick={() => void openExternal(url)}>
        Sign in
      </Button>
    )
  } else if (action?.kind === "owner") {
    control = <ProfileForm busy={busy} onAnswer={(url) => answer({ linkedinUrl: url })} />
  } else if (stopped) {
    control = (
      <Button variant="primary" disabled={busy} onClick={() => answer({})}>
        {data.status === "failed" ? "Try again" : "Continue setup"}
      </Button>
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
