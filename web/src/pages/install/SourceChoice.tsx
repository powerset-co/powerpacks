import { useState } from "react"

import { installAction } from "@/lib/api/install"
import { errorText } from "@/lib/api/http"
import type { InstallStatus } from "@/types/install"

const SOURCES = ["gmail", "imessage", "whatsapp", "linkedin"]
const NAMES: Record<string, string> = {
  gmail: "Gmail",
  imessage: "iMessage",
  whatsapp: "WhatsApp",
  linkedin: "LinkedIn",
}

export function SourceChoice({ data }: { data: InstallStatus }) {
  const [sources, setSources] = useState<string[]>(
    data.action?.kind === "gmail"
      ? SOURCES.filter((source) => data.plan?.some((step) => step.startsWith(source)))
      : [],
  )
  const [emails, setEmails] = useState("")
  const [years, setYears] = useState("3")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  async function submit(skip = false) {
    setBusy(true)
    setError("")
    const after = new Date()
    after.setFullYear(after.getFullYear() - Number(years))
    try {
      await installAction("sources", {
        sources: skip ? [] : sources,
        skip,
        gmail_emails: emails.split(/[\s,]+/).filter(Boolean),
        sync_after: years === "all" ? "2004-01-01" : after.toISOString().slice(0, 10),
      })
    } catch (caught) {
      setError(errorText(caught))
      setBusy(false)
    }
  }
  return (
    <form
      className="install-choice"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <fieldset disabled={busy}>
        <legend>Add the contacts you want</legend>
        <div className="install-source-options">
          {SOURCES.map((source) => (
            <label key={source}>
              <input
                type="checkbox"
                checked={sources.includes(source)}
                onChange={(event) => {
                  setSources(
                    event.target.checked ? [...sources, source] : sources.filter((item) => item !== source),
                  )
                }}
              />
              {NAMES[source]}
            </label>
          ))}
        </div>
        {sources.includes("gmail") ? (
          <div className="install-gmail-choice">
            <label>
              Gmail accounts
              <input
                required
                value={emails}
                onChange={(event) => setEmails(event.target.value)}
                placeholder="you@gmail.com"
              />
            </label>
            <label>
              History
              <select value={years} onChange={(event) => setYears(event.target.value)}>
                <option value="1">1 year</option>
                <option value="3">3 years</option>
                <option value="5">5 years</option>
                <option value="all">All mail</option>
              </select>
            </label>
            <p>Mail stays on your Mac. More history takes longer.</p>
          </div>
        ) : null}
        <div className="install-buttons">
          <button type="submit" disabled={!sources.length}>
            {busy ? "Starting…" : "Continue"}
          </button>
          <button type="button" onClick={() => void submit(true)}>
            Skip for now
          </button>
        </div>
      </fieldset>
      {error ? <p role="alert">{error}</p> : null}
    </form>
  )
}
