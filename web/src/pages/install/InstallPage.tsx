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
  running: "Setting up Powerpacks",
  waiting: "One thing to finish",
  failed: "Setup needs a fix",
  completed: "Powerpacks is installed",
  skipped: "Setting up Powerpacks",
}
const STEPS: { step: InstallStep; label: string }[] = [
  { step: "runtime", label: "Prepare your Mac" },
  { step: "dependencies", label: "Install what Powerpacks needs" },
  { step: "skills", label: "Add Powerpacks to your agent" },
  { step: "account", label: "Sign in to Powerset" },
  { step: "credentials", label: "Connect search services" },
  { step: "connection", label: "Connect your agent to Powerset" },
  { step: "network", label: "Check your network" },
]
const STATUS_LABELS: Record<InstallState, string> = {
  running: "In progress",
  waiting: "Waiting for you",
  failed: "Needs a fix",
  completed: "Done",
  skipped: "Skipped",
}

export function InstallPage() {
  const { data, error } = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const title = error
    ? "Reconnecting to Powerpacks"
    : data?.status === "completed" && data.network_name && (data.person_count ?? 0) > 0
      ? "Powerpacks is ready"
      : data?.status === "waiting" && data.step === "account"
        ? "Waiting for you to sign in"
        : data
          ? TITLES[data.status]
          : "Opening Powerpacks"
  const running = !error && data?.status === "running"
  const steps = data?.steps.tools
    ? [...STEPS.slice(0, 3), { step: "tools" as const, label: "Prepare Gmail import" }, ...STEPS.slice(3)]
    : STEPS
  const part = steps.filter(({ step }) =>
    ["completed", "skipped"].includes(data?.steps[step]?.status ?? ""),
  ).length

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
          <EmptyPanel title={title} above={<EnrichMark part={part} parts={steps.length} running={running} />}>
            <p className="install-message">
              {error
                ? "The page will reconnect automatically. If it stays here, tell me in chat so I can check the server."
                : data?.status === "failed"
                  ? "This step didn't finish. Your completed work is saved."
                  : (data?.message ?? "Reading your installation progress…")}
            </p>
            {data?.status !== "waiting" ? (
              <p className="install-note">
                {error
                  ? "You can keep this page open."
                  : data?.status === "completed"
                    ? data.network_name
                      ? 'Try asking in chat: "Find backend engineers."'
                      : "Installation is complete. Account connection was not requested."
                    : data?.status === "failed"
                      ? "Your agent can read the saved error and retry this step. Check chat for the next action."
                      : "You can stay here. I'll let you know when I need you."}
              </p>
            ) : null}
          </EmptyPanel>
        </section>
        {data ? (
          <div className="install-account">
            {data.account_email ? <p>{data.account_email}</p> : null}
            {data.network_name ? (
              <p>
                {data.network_name}
                {data.person_count != null ? ` · ${data.person_count.toLocaleString()} people` : ""}
              </p>
            ) : null}
          </div>
        ) : null}
        {data ? (
          <ol className="install-steps" aria-label="Setup steps">
            {steps.map(({ step, label }) => {
              const progress = data.steps[step]
              const done = progress?.status === "completed"
              const current = data.step === step
              return (
                <li key={step} data-done={done} aria-current={current ? "step" : undefined}>
                  <span className="install-step-mark" aria-hidden="true">
                    {done ? "✓" : progress?.status === "skipped" ? "−" : current ? "•" : "○"}
                  </span>
                  <div>
                    <span>{label}</span>
                    {progress?.status === "skipped" ? (
                      <p className="install-step-message">{progress.message}</p>
                    ) : null}
                  </div>
                  <span className="install-step-status">
                    {progress ? STATUS_LABELS[progress.status] : "Not started"}
                  </span>
                </li>
              )
            })}
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
