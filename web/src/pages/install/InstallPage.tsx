import "../review/styles/base.css"
import "./install.css"

import { useQuery } from "@tanstack/react-query"
import { useEffect } from "react"

import { fetchInstall } from "@/lib/api/install"
import { EmptyPanel } from "@/pages/review/shared/EmptyPanel"
import { EnrichMark } from "@/pages/review/shared/EnrichMark"
import type { InstallState, InstallStep } from "@/types/install"

const POLL_MS = 1_500
const TITLES: Record<InstallState, string> = {
  running: "Installing Powerpacks",
  waiting: "Over to you",
  failed: "Installation needs a fix",
  completed: "Powerpacks is installed",
}
const STEPS: { step: InstallStep; label: string }[] = [
  { step: "runtime", label: "Prepare your Mac" },
  { step: "dependencies", label: "Install what Powerpacks needs" },
  { step: "skills", label: "Add your skills" },
]

export function InstallPage() {
  const { data, error } = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const title = error ? "Reconnecting to Powerpacks" : data ? TITLES[data.status] : "Opening Powerpacks"
  const running = !error && data?.status === "running"
  const part =
    data?.step === "ready" || data?.step === "tools"
      ? STEPS.length
      : STEPS.findIndex(({ step }) => step === data?.step)

  useEffect(() => {
    document.title = `${title} · Powerpacks`
  }, [title])

  return (
    <div className="review-page install-page" data-status={error ? "disconnected" : data?.status}>
      <header className="topbar">
        <span className="brand">POWERPACKS</span>
        <h1 className="topbar-title">Getting started</h1>
        <span />
      </header>
      <main className="install-main">
        <section aria-live="polite" aria-atomic="true">
          <EmptyPanel title={title} above={<EnrichMark part={part} parts={STEPS.length} running={running} />}>
            <p className="install-message">
              {error
                ? "The page will reconnect automatically. If it stays here, tell me in chat so I can check the server."
                : data?.status === "failed"
                  ? "This step didn't finish. Your completed work is saved."
                  : (data?.message ?? "Reading your installation progress…")}
            </p>
            <p className="install-note">
              {error
                ? "You can keep this page open."
                : data?.status === "completed"
                  ? "Tell me in chat what you'd like to connect: LinkedIn, Gmail, iMessage, or WhatsApp."
                  : data?.status === "failed"
                    ? "Tell me to fix it in chat. I can read the install log and retry."
                    : data?.status === "waiting"
                      ? "Reply in chat when you're ready. I'll continue from here."
                      : "You can stay here. I'll let you know when I need you."}
            </p>
          </EmptyPanel>
        </section>
        {data ? (
          <ol className="install-steps" aria-label="Installation steps">
            {STEPS.map(({ step, label }, index) => (
              <li key={step} data-done={index < part} aria-current={index === part ? "step" : undefined}>
                <span aria-hidden="true">{index < part ? "✓" : index + 1}</span>
                {label}
              </li>
            ))}
          </ol>
        ) : null}
        {data ? (
          <details className="install-details">
            <summary>Details for troubleshooting</summary>
            {data.status === "failed" ? <p>{data.message}</p> : null}
            <p>
              Install log: <code>{data.log_path}</code>
            </p>
            <p>
              Retry: <code>{data.retry_command}</code>
            </p>
            <p>You can ask me to check these in chat.</p>
          </details>
        ) : null}
      </main>
    </div>
  )
}
