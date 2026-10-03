import "../review/styles/base.css"
import "./install.css"

import { useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { fetchInstall, installAction } from "@/lib/api/install"
import { fetchStatus } from "@/lib/api/review"
import { errorText } from "@/lib/api/http"
import { EmptyPanel } from "@/pages/review/shared/EmptyPanel"
import { EnrichMark } from "@/pages/review/shared/EnrichMark"
import { doingNow } from "@/pages/review/enrich/copy"
import type { InstallState } from "@/types/install"

import { SourceChoice } from "./SourceChoice"

const DEFAULT_STEPS = ["runtime", "dependencies", "skills", "account", "credentials", "connection", "network"]
const DEFAULT_LABELS: Record<string, string> = {
  runtime: "Prepare your Mac",
  dependencies: "Install Powerpacks",
  skills: "Add your skills",
  account: "Sign in",
  credentials: "Connect search",
  connection: "Connect your agent",
  network: "Check your network",
}
const TITLES: Record<InstallState, string> = {
  running: "Setting up Powerpacks",
  waiting: "One thing to finish",
  failed: "Setup needs a fix",
  completed: "Powerpacks is installed",
  skipped: "Setting up Powerpacks",
}
const STATUS_LABELS: Record<InstallState, string> = {
  running: "Working",
  waiting: "Waiting",
  failed: "Needs a fix",
  completed: "Done",
  skipped: "Skipped",
}
const DONE = new Set(["completed", "skipped"])
const VISIBLE_COMPLETED = 5

export function InstallPage() {
  const { data, error } = useQuery({
    queryKey: ["install"],
    queryFn: ({ signal }) => fetchInstall(signal),
    refetchInterval: 1_500,
    retry: false,
  })
  const [expanded, setExpanded] = useState(false)
  const [actionError, setActionError] = useState("")
  const failed = data?.status === "failed" || data?.index_progress?.status === "failed"
  const { data: processing } = useQuery({
    queryKey: ["review-status"],
    queryFn: ({ signal }) => fetchStatus(signal),
    enabled: data?.step === "deep_context" && data.status === "running",
    refetchInterval: 5_000,
    retry: false,
  })
  const title = error
    ? "Reconnecting to Powerpacks"
    : failed
      ? TITLES.failed
      : data?.status === "completed" && data.network_name && (data.person_count ?? 0) > 0
        ? "Powerpacks is ready"
        : data?.status === "waiting" && data.step === "account"
          ? "Waiting for you to sign in"
          : data?.status === "waiting" && data.action?.kind === "processing"
            ? "Your contacts are saved"
            : data
              ? TITLES[data.status]
              : "Opening Powerpacks"
  const steps = data?.plan ?? DEFAULT_STEPS
  const completed = steps.filter((step) => DONE.has(data?.steps[step]?.status ?? ""))
  const folded = completed.slice(0, -VISIBLE_COMPLETED)
  const currentIndex = steps.indexOf(data?.step ?? "")
  const next = steps.slice(currentIndex + 1).find((step) => !data?.steps[step])
  const action = data?.action
  useEffect(() => {
    document.title = `${title} · Powerpacks`
  }, [title])
  async function open(action: string) {
    try {
      await installAction(action)
    } catch (caught) {
      setActionError(errorText(caught))
    }
  }
  return (
    <div
      className="review-page install-page"
      data-status={error ? "disconnected" : failed ? "failed" : data?.status}
    >
      <header className="topbar">
        <span className="brand">POWERPACKS</span>
        <h1 className="topbar-title">Getting started</h1>
        <span />
      </header>
      <main className="install-main">
        <section aria-live="polite">
          <EmptyPanel
            title={title}
            above={
              <EnrichMark
                part={completed.length}
                parts={Math.max(steps.length, 1)}
                running={!error && !failed && data?.status === "running"}
              />
            }
          >
            <p className="install-message">
              {error
                ? "Reconnecting automatically…"
                : failed
                  ? "Your progress is saved. I can check this step and retry."
                  : data?.step === "deep_context" && processing?.stage === "enrich"
                    ? doingNow(processing.step, processing.pending)
                    : data?.step === "index" && data.index_progress
                      ? data.index_progress.message
                      : (data?.message ?? "Reading your progress…")}
            </p>
            <p className="install-note">
              {data?.status === "completed"
                ? "You can keep asking here in chat."
                : "I’ll keep going. Ask questions or give me input in chat."}
            </p>
          </EmptyPanel>
          {data?.step === "index" && data.index_progress?.progress != null && !failed ? (
            <progress
              className="install-index-progress"
              aria-label="Search index progress"
              max={1}
              value={data.index_progress.progress}
            />
          ) : null}
        </section>
        {data?.status === "waiting" && action ? (
          <section className="install-action">
            {action.kind === "sources" || (action.kind === "gmail" && !action.command) ? (
              <SourceChoice key={action.kind} data={data} />
            ) : null}
            {action.kind === "gmail" && action.command ? (
              <p>Finish connecting Gmail in your browser. I’ll continue here.</p>
            ) : null}
            {action.kind === "qr" ? (
              <div className="install-qr">
                {action.qr_url ? (
                  <img src={action.qr_url} alt="Scan this QR code to link WhatsApp" />
                ) : (
                  <p>Getting your QR code…</p>
                )}
                <p>WhatsApp → Settings → Linked devices → Link a device</p>
              </div>
            ) : null}
            {action.kind === "permission" ? (
              <div className="install-permission">
                <p>
                  Enable Full Disk Access for{" "}
                  <strong>
                    {action.app_path?.split("/").pop()?.replace(".app", "") ?? "the app running this session"}
                  </strong>
                  .
                </p>
                {action.app_path ? <code>{action.app_path}</code> : null}
                <button type="button" onClick={() => void open("permissions")}>
                  Open settings &amp; show the app
                </button>
                <p>Drag the highlighted app into Full Disk Access, enable it, then tell me here.</p>
              </div>
            ) : null}
            {action.kind === "linkedin" ? (
              <div>
                <button type="button" onClick={() => void open("linkedin")}>
                  Open LinkedIn
                </button>
                <p>Send me Connections.csv here when it arrives.</p>
              </div>
            ) : null}
            {action.kind === "processing" ? (
              <p>Your contacts are saved. I’ll check what’s needed to make them searchable.</p>
            ) : null}
            {actionError ? <p role="alert">{actionError}</p> : null}
          </section>
        ) : null}
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
          <>
            {folded.length > 0 ? (
              <button
                className="install-history"
                type="button"
                aria-expanded={expanded}
                onClick={() => setExpanded(!expanded)}
              >
                {expanded ? "Hide earlier steps" : `${folded.length} earlier steps done`}
              </button>
            ) : null}
            <ol className="install-steps" aria-label="Setup steps">
              {steps.map((step) => {
                const progress = data.steps[step]
                const current = data.step === step
                const done = DONE.has(progress?.status ?? "")
                const hidden =
                  (folded.includes(step) && !expanded) || (!progress && !current && step !== next)
                return (
                  <li
                    key={step}
                    data-folded={hidden}
                    data-done={done}
                    aria-hidden={hidden}
                    aria-current={current ? "step" : undefined}
                  >
                    <div className="install-step-row">
                      <span className="install-step-mark" aria-hidden="true">
                        {progress?.status === "completed"
                          ? "✓"
                          : progress?.status === "skipped"
                            ? "−"
                            : current
                              ? "•"
                              : "○"}
                      </span>
                      <span>{data.labels?.[step] ?? DEFAULT_LABELS[step] ?? step}</span>
                      <span className="install-step-status">
                        {current && failed
                          ? STATUS_LABELS.failed
                          : progress
                            ? STATUS_LABELS[progress.status]
                            : current
                              ? STATUS_LABELS[data.status]
                              : "Next"}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ol>
            <details className="install-details">
              <summary>Details</summary>
              {failed ? <p>{data.index_progress?.message ?? data.message}</p> : null}
              <p>
                Log: <code>{data.log_path}</code>
              </p>
              <p>
                Retry: <code>{data.retry_command}</code>
              </p>
              {action?.details ? <pre>{JSON.stringify(action.details, null, 2)}</pre> : null}
              {data.index_progress?.payload ? (
                <pre>{JSON.stringify(data.index_progress.payload, null, 2)}</pre>
              ) : null}
            </details>
          </>
        ) : null}
      </main>
    </div>
  )
}
