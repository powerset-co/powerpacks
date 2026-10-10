import "../review/styles/base.css"
import "./install.css"

import { useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { fetchInstall, installAction } from "@/lib/api/install"
import { fetchLinkedinCard } from "@/lib/api/review"
import { isDesktop } from "@/lib/desktop"
import { errorText } from "@/lib/api/http"
import { ReservedLines } from "@/components/shared/ReservedLines"
import { EmptyPanel } from "@/pages/review/shared/EmptyPanel"
import { EnrichMark } from "@/pages/review/shared/EnrichMark"
import type { InstallAction, InstallState, InstallStatus } from "@/types/install"

import { DesktopSetup } from "./DesktopSetup"
import { DesktopWelcome } from "./DesktopWelcome"

// Every other word comes with the status (status_prose.py); these show while the page cannot reach setup.
const OFFLINE = {
  title: "Reconnecting to Powerpacks",
  line: "Reconnecting automatically…",
  opening: "Opening Powerpacks",
  reading: "Reading your progress…",
}
// The status line and the note under it hold this many lines, so the page never jumps
// (status_prose.py keeps every line and note within them).
const MESSAGE_LINES = { wide: 3, narrow: 5 }
const NOTE_LINES = { wide: 4, narrow: 6 }
const DONE = new Set(["completed", "skipped"])
const VISIBLE_COMPLETED = 5

/** Seconds as the page counts them: 42s, 3m 05s, 1h 12m. */
function elapsedText(seconds: number): string {
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`
  if (minutes) return `${minutes}m ${String(rest).padStart(2, "0")}s`
  return `${rest}s`
}

/** How long the current step has run, ticking every second while setup works; "" otherwise. */
function useElapsed(startedAt: string | undefined, running: boolean): string {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running || !startedAt) return
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [running, startedAt])
  if (!running || !startedAt) return ""
  const started = Date.parse(startedAt)
  if (Number.isNaN(started)) return ""
  return elapsedText(Math.max(0, Math.floor((now - started) / 1000)))
}

function installSteps(data?: InstallStatus) {
  if (!data) return []
  const plan = data.plan ?? []
  return data.prose.rows.flatMap((row) => {
    const members = plan.filter((step) => row.steps.includes(step))
    if (!members.length || (row.needs && !plan.includes(row.needs))) return []
    const current = members.includes(data.step)
    const states = members.map((step) => data.steps[step]?.status)
    const latest = [...states].reverse().find((state) => state != null)
    const complete = states.every((state) => DONE.has(state ?? ""))
    const status: InstallState | undefined = current
      ? DONE.has(data.status) && !complete
        ? "running"
        : data.status
      : complete
        ? states.every((state) => state === "skipped")
          ? "skipped"
          : "completed"
        : latest && DONE.has(latest)
          ? undefined // part done in an earlier run; the rest waits its turn
          : latest
    return [
      {
        key: row.label,
        label: row.label,
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
  // Before the first click: only the welcome and the choice, none of the progress furniture.
  const welcome = !error && data?.event === "setup.ready"
  const failed = data?.status === "failed" || data?.index_progress?.status === "failed"
  const word = (key: string) => data?.prose.page[key] ?? ""
  const title = error
    ? OFFLINE.title
    : !data
      ? OFFLINE.opening
      : word(
          failed
            ? "title.failed"
            : data.event === "setup.ready"
              ? "title.welcome"
              : data.action?.kind === "resume"
                ? "title.paused"
                : data.step === "ready" &&
                    data.status === "completed" &&
                    data.network_name &&
                    (data.person_count ?? 0) > 0
                  ? "title.ready"
                  : data.status === "waiting" && data.step === "account"
                    ? "title.signing_in"
                    : data.status === "waiting" && data.action?.kind === "processing"
                      ? "title.processing"
                      : data.status === "completed" && data.step !== "ready"
                        ? "title.running"
                        : data.status === "skipped"
                          ? "title.running"
                          : `title.${data.status}`,
        )
  // The matches left to check, counted by the review queue the review page reads.
  const ready = data?.step === "ready" && data.status === "completed"
  const { data: linkedin } = useQuery({
    queryKey: ["linkedin-left"],
    queryFn: ({ signal }) => fetchLinkedinCard({}, signal),
    enabled: ready,
    refetchInterval: 15_000,
    retry: false,
  })
  const reviewLeft = ready ? (linkedin?.pending ?? 0) : 0
  const steps = installSteps(data)
  const completed = steps.filter((step) => DONE.has(step.status ?? ""))
  const folded = completed.slice(0, -VISIBLE_COMPLETED)
  const currentIndex = steps.findIndex((step) => step.current)
  const next = steps.slice(currentIndex + 1).find((step) => !step.status)
  const action: InstallAction | undefined =
    reviewLeft > 0
      ? {
          kind: "review",
          text:
            reviewLeft === 1
              ? word("review.offer.one")
              : word("review.offer.many").replace("{count}", reviewLeft.toLocaleString()),
        }
      : (data?.action ?? undefined)
  const message = error
    ? OFFLINE.line
    : data?.step === "index" && data.index_progress
      ? data.index_progress.message
      : (data?.message ?? OFFLINE.reading)
  const note = error ? "" : failed ? word("failed.note") : data?.note
  // The live line under a running step: how long it has run, beside the counts in its message.
  const elapsed = useElapsed(data?.step_started_at, !error && !failed && data?.status === "running")
  useEffect(() => {
    document.title = `${title} · Powerpacks`
  }, [title])
  async function open(action: string) {
    // The app shows the review in its own window; the server would open a browser tab.
    if (action === "review" && isDesktop()) {
      window.location.assign("/?stage=linkedin")
      return
    }
    try {
      await installAction(action)
    } catch (caught) {
      setActionError(errorText(caught))
    }
  }
  // The desktop app's own first two screens; setup's progress below is the third.
  if (welcome && isDesktop()) {
    return (
      <div className="review-page install-page overflow-y-auto" data-welcome>
        <main className="install-main rise-in">
          <DesktopWelcome />
        </main>
      </div>
    )
  }
  return (
    <div
      className="review-page install-page overflow-y-auto"
      data-status={error ? "disconnected" : failed ? "failed" : data?.status}
      data-welcome={welcome || undefined}
    >
      {/* The welcome and the progress are two layouts: the new one rises in, never a jump. */}
      <main key={welcome ? "welcome" : "progress"} className="install-main rise-in">
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
            {welcome ? (
              <p className="install-message">Start fresh, or bring over what you already have on this Mac.</p>
            ) : (
              <ReservedLines
                className="install-message"
                lines={MESSAGE_LINES.wide}
                narrowLines={MESSAGE_LINES.narrow}
              >
                {/* A new line fades in over the old one's slot. */}
                <span key={message} className="fade-swap">
                  {message}
                </span>
              </ReservedLines>
            )}
            {/* A clock ticks every second: kept out of the live region's announcements. */}
            {welcome ? null : (
              <p className="install-elapsed" aria-hidden="true">
                {elapsed ? word("running.elapsed").replace("{elapsed}", elapsed) : ""}
              </p>
            )}
            {welcome ? null : (
              <ReservedLines className="install-note" lines={NOTE_LINES.wide} narrowLines={NOTE_LINES.narrow}>
                {note}
              </ReservedLines>
            )}
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
        {action && (data?.status === "waiting" || action.kind === "review") ? (
          <section className="install-action">
            {action.kind === "qr" ? (
              <div className="install-qr">
                {action.qr_url ? (
                  <img src={action.qr_url} alt={word("qr.alt")} />
                ) : (
                  <p>{word("qr.loading")}</p>
                )}
                <p>{word("qr.where")}</p>
              </div>
            ) : null}
            {action.kind === "permission" ? (
              <button type="button" onClick={() => void open("permissions")}>
                {word("permission.button")}
              </button>
            ) : null}
            {action.kind === "review" ? (
              <>
                <p className="install-review-offer">{action.text}</p>
                <button type="button" onClick={() => void open("review")}>
                  {word("review.button")}
                </button>
              </>
            ) : null}
            {actionError ? <p role="alert">{actionError}</p> : null}
          </section>
        ) : null}
        {data && !error && isDesktop() ? <DesktopSetup data={data} /> : null}
        {data && !welcome ? (
          <>
            {/* The button keeps its slot even with nothing folded, so the list never moves. */}
            <button
              className="install-history"
              type="button"
              aria-expanded={expanded}
              aria-hidden={folded.length === 0}
              tabIndex={folded.length === 0 ? -1 : undefined}
              data-empty={folded.length === 0}
              onClick={() => setExpanded(!expanded)}
            >
              {expanded
                ? word("history.open")
                : word("history.folded").replace("{count}", String(folded.length))}
            </button>
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
                        {word(
                          current && data.event === "setup.ready"
                            ? "state.next"
                            : current && (error || data.action?.kind === "resume")
                              ? "state.paused"
                              : current && failed
                                ? "state.failed"
                                : progress
                                  ? `state.${progress}`
                                  : current
                                    ? `state.${data.status}`
                                    : "state.next",
                        )}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ol>
          </>
        ) : null}
      </main>
    </div>
  )
}
