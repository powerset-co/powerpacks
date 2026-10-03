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
import type { InstallState, InstallStatus } from "@/types/install"

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
const SOURCE_STEPS = [
  { key: "imessage_access", label: "Link iMessage", steps: ["imessage_access"] },
  { key: "imessage_import", label: "Sync iMessage", steps: ["imessage_import"] },
  { key: "whatsapp_login", label: "Link WhatsApp", steps: ["whatsapp_tools", "whatsapp_login"] },
  { key: "whatsapp_sync", label: "Sync WhatsApp", steps: ["whatsapp_sync", "whatsapp_import"] },
  { key: "gmail_login", label: "Link Gmail", steps: ["gmail_tools", "gmail_login"] },
  { key: "gmail_sync", label: "Sync Gmail", steps: ["gmail_sync", "gmail_import"] },
]

function installSteps(data?: InstallStatus) {
  const plan = data?.plan ?? DEFAULT_STEPS
  return plan.flatMap((step) => {
    const group = SOURCE_STEPS.find((group) => group.steps.includes(step))
    const members = group ? plan.filter((step) => group.steps.includes(step)) : [step]
    if (step !== members[0]) return []
    const current = members.includes(data?.step ?? "")
    const states = members.map((step) => data?.steps[step]?.status)
    const latest = [...states].reverse().find((state) => state != null)
    const complete = states.every((state) => DONE.has(state ?? ""))
    const status: InstallState | undefined = current
      ? group && DONE.has(data?.status ?? "") && !complete
        ? "running"
        : data?.status
      : complete
        ? states.every((state) => state === "skipped")
          ? "skipped"
          : "completed"
        : latest && DONE.has(latest)
          ? "running"
          : latest
    return [
      {
        key: group?.key ?? step,
        label: group?.label ?? data?.labels?.[step] ?? DEFAULT_LABELS[step] ?? step,
        current,
        status,
      },
    ]
  })
}

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
  const steps = installSteps(data)
  const completed = steps.filter((step) => DONE.has(step.status ?? ""))
  const folded = completed.slice(0, -VISIBLE_COMPLETED)
  const currentIndex = steps.findIndex((step) => step.current)
  const next = steps.slice(currentIndex + 1).find((step) => !step.status)
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
                  : data?.step === "deep_context" &&
                      data.status === "running" &&
                      processing?.stage === "enrich" &&
                      processing.step
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
            {action.kind === "sources" ? <SourceChoice key={action.kind} data={data} /> : null}
            {action.kind === "gmail" && !action.command ? (
              <p>Tell me which Gmail account to use in chat.</p>
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
              <p>
                {action.text ?? "Your contacts are saved. I’ll check what’s needed to make them searchable."}
              </p>
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
                {expanded ? "Hide completed tasks" : `${folded.length} tasks completed`}
              </button>
            ) : null}
            <ol className="install-steps" aria-label="Setup steps">
              {steps.map((step) => {
                const progress = step.status
                const current = step.current
                const done = DONE.has(progress ?? "")
                const hidden =
                  (folded.includes(step) && !expanded) || (!progress && !current && step !== next)
                return (
                  <li
                    key={step.key}
                    data-folded={hidden}
                    data-done={done}
                    aria-hidden={hidden}
                    aria-current={current ? "step" : undefined}
                  >
                    <div className="install-step-row">
                      <span className="install-step-mark" aria-hidden="true">
                        {progress === "completed" ? "✓" : progress === "skipped" ? "−" : current ? "•" : "○"}
                      </span>
                      <span>{step.label}</span>
                      <span className="install-step-status">
                        {current && failed
                          ? STATUS_LABELS.failed
                          : progress
                            ? STATUS_LABELS[progress]
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
