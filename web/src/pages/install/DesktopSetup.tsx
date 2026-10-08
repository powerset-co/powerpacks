import { useState, type FormEvent } from "react"

import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, type SetupAnswer } from "@/lib/api/install"
import { isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

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

/** In the desktop app nobody runs setup's commands in chat: its waits get buttons here. */
export function DesktopSetup({ data }: { data: InstallStatus }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const action = data.action

  const answer = (value: SetupAnswer) => {
    setBusy(true)
    setError(null)
    continueSetup(value)
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setBusy(false))
  }

  let control
  if (action?.kind === "approval" && action.step) {
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
  } else if (
    data.status === "failed" ||
    (data.status === "waiting" && !["qr", "permission", "review"].includes(action?.kind ?? ""))
  ) {
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
