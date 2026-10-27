import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import { useLocation } from "react-router-dom"

import { CheckIcon, ProgressBar, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, fetchFirstLook, fetchInstall } from "@/lib/api/install"
import { cn, isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

import { Frame, Info, Note } from "./Frame"
import { leftText, secondsLeft, type Sample } from "./pace"
import { FirstLookPanel } from "./FirstLookPanel"
import { PageShot } from "./PageShot"

const POLL_MS = 1_500
const PEOPLE_POLL_MS = 5_000
const KEY = ["onboarding", "install"] as const

// The stages setup goes through (steps.py), as the person sees them. The source stages show only
// once their source is connected: the first run is LinkedIn alone, the next adds what Sources linked.
const STAGES = [
  { steps: ["linkedin"], label: "Reading your connections", done: "Connections read", source: false },
  {
    steps: ["gmail_tools", "gmail_login", "gmail_sync", "gmail_import"],
    label: "Syncing your Gmail",
    done: "Gmail contacts added",
    source: true,
  },
  {
    steps: ["imessage_access", "imessage_import"],
    label: "Reading your Messages",
    done: "iMessage contacts added",
    source: true,
  },
  {
    steps: ["whatsapp_tools", "whatsapp_login", "whatsapp_sync", "whatsapp_import"],
    label: "Syncing WhatsApp",
    done: "WhatsApp contacts added",
    source: true,
  },
  { steps: ["deep_context"], label: "Learning who they are", done: "Learned who they are", source: false },
  { steps: ["enrich"], label: "Enriching people", done: "People enriched", source: false },
  {
    steps: ["index", "validate"],
    label: "Building your search index",
    done: "Search index built",
    source: false,
  },
  { steps: ["ready"], label: "Search is ready", done: "Search is ready", source: false },
] as const
type Stage = (typeof STAGES)[number]
const DONE = new Set(["completed", "skipped"])
// The stages that read messages: only a run with a message source lists them.
const MESSAGE_STAGES = new Set<string>(["deep_context", "enrich"])

interface Progress {
  data: InstallStatus
  samples: Sample[]
}

/** The running step's count, from the event's details (workflow.py linkedin.reading.count writes
 *  read/total; pipeline.py's stage counters write done/total): a pace for the time left. The index
 *  has no count (Modal reports phases, not work done), so it shows its phase alone. */
function counts(data: InstallStatus | undefined): { read: number; total: number } | null {
  const details = data?.action?.details
  if (!isRecord(details)) return null
  const read = details.read ?? details.done
  const { total } = details
  return typeof read === "number" && typeof total === "number" && total > 0 ? { read, total } : null
}

type Standing = "done" | "skipped" | "current" | "stopped" | "next"

/** Whether a stage has anything in the record: its step is running or has run. */
function present(data: InstallStatus, stage: Stage): boolean {
  return stage.steps.some((step) => step === data.step || step in data.steps)
}

/** Where one stage stands: done, the one under way, stopped there, or still to come. A source stage
 *  runs only the steps it needs (a tool already installed writes none), so its present steps decide. */
function standingOf(data: InstallStatus, stage: Stage): Standing {
  const current = stage.steps.some((step) => step === data.step)
  if (current) {
    if (data.status === "failed") return "stopped"
    if (data.event === "search.ready" || (stage.steps[0] === "ready" && DONE.has(data.status))) return "done"
    return "current"
  }
  const steps = stage.source ? stage.steps.filter((step) => step in data.steps) : stage.steps
  const states = steps.map((step) => data.steps[step]?.status)
  if (states.length === 0 || !states.every((state) => DONE.has(state ?? ""))) return "next"
  return states.every((state) => state === "skipped") ? "skipped" : "done"
}

/** What a finished stage says: the record's own line for its last step (e.g. "293 LinkedIn
 *  connections", "Your LinkedIn connections are in; nothing to learn from messages yet."). The index
 *  and ready stages say their own words: the record's line for both is the ready line. */
function finishedLine(data: InstallStatus, stage: Stage): string {
  if (stage.steps[0] === "index" || stage.steps[0] === "ready") return stage.done
  const last = [...stage.steps].reverse().find((step) => data.steps[step]?.message)
  return last ? (data.steps[last]?.message ?? stage.done) : stage.done
}

/** Set up: setup runs in the background (bin/onboard) from the moment this opens; the stages
 *  show as they go, the LinkedIn count with the time left, the index with Modal's progress. The
 *  first look opens once people are in. */
export function SetUp() {
  const client = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  // Try again after a stop; the spinner stays until the record shows the run.
  const [retrying, setRetrying] = useState(false)
  // The record, with the LinkedIn count sampled at every read: the pace so far gives the time left.
  const install = useQuery({
    queryKey: KEY,
    queryFn: async ({ signal }): Promise<Progress> => {
      const data = await fetchInstall(signal)
      const before = client.getQueryData<Progress>(KEY)?.samples ?? []
      const count = counts(data)
      const samples = !count
        ? []
        : before[before.length - 1]?.done === count.read
          ? before
          : [...before, { at: Date.now(), done: count.read }]
      return { data, samples }
    },
    refetchInterval: POLL_MS,
    retry: false,
  })
  // Opened by Continue: the record is the last run's until the new one writes, so a record from
  // before the click is not shown (the moment the run starts, it replaces it).
  const state: unknown = useLocation().state
  const startedAt = isRecord(state) && typeof state.startedAt === "number" ? state.startedAt : undefined
  const record = install.data?.data
  const data =
    record && startedAt !== undefined && Date.parse(record.updated_at) < startedAt ? undefined : record
  const running = data?.status === "running" || (data?.status === "waiting" && (data.installer_pid ?? 0) > 0)
  const failed = data?.status === "failed"
  const ready = data?.event === "search.ready"
  const count = counts(data)
  const left = count ? secondsLeft(install.data?.samples ?? [], count.total) : null
  const processing = data !== undefined && data.step !== "linkedin" && !ready

  // Nothing running and nothing stopped: this screen starts setup itself, once.
  const started = useRef(false)
  useEffect(() => {
    if (!data || running || failed || ready || started.current) return
    started.current = true
    continueSetup({}).catch((caught: unknown) => setError(errorText(caught)))
  }, [data, running, failed, ready])

  // People appear as the index is built; once there are some the first look takes the right column.
  const look = useQuery({
    queryKey: ["onboarding", "first-look"],
    queryFn: ({ signal }) => fetchFirstLook(signal),
    enabled: processing || ready,
    refetchInterval: processing ? PEOPLE_POLL_MS : false,
    retry: false,
  })
  const known = look.data?.total ?? 0

  const retry = () => {
    setRetrying(true)
    setError(null)
    continueSetup({}).catch((caught: unknown) => {
      setError(errorText(caught))
      setRetrying(false)
    })
  }

  // The run's first steps (account, keys, sources) carry the last run's stage entries until the
  // selection starts them over: the list shows once the run is on a stage.
  const underway =
    data !== undefined && (ready || STAGES.some((stage) => stage.steps.some((step) => step === data.step)))
  // The long run: a message source is in, so deep context reads it.
  const long = data !== undefined && STAGES.some((stage) => stage.source && present(data, stage))
  // With LinkedIn alone there is nothing to learn from messages: those stages are not listed.
  const stages =
    data && underway
      ? STAGES.filter(
          (stage) =>
            (!stage.source || present(data, stage)) &&
            (long || !MESSAGE_STAGES.has(stage.steps[0])) &&
            standingOf(data, stage) !== "skipped",
        )
      : []
  const current = data ? stages.find((stage) => standingOf(data, stage) === "current") : undefined
  const indexProgress = data?.step === "index" ? data.index_progress : null
  return (
    <Frame
      step="setup"
      left={
        <>
          <h1 className="m-0 text-[22px] font-bold leading-tight">
            {failed ? "Setup stopped" : ready ? "Search is ready" : (current?.label ?? "Setting up")}
          </h1>
          {install.error ? <Note bad>{errorText(install.error)}</Note> : null}
          {data && underway ? (
            <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Setup stages">
              {stages.map((stage) => {
                const standing = standingOf(data, stage)
                const live = standing === "current" || standing === "stopped"
                return (
                  <li key={stage.label} className="flex flex-col gap-2" data-stage={standing}>
                    <div
                      className={cn(
                        "flex items-center gap-3 text-[13px] transition-colors duration-med",
                        standing === "next"
                          ? "text-faint"
                          : standing === "skipped"
                            ? "text-muted-foreground"
                            : "text-foreground",
                      )}
                    >
                      <span className="grid size-5 shrink-0 place-items-center" aria-hidden>
                        {standing === "done" ? (
                          <CheckIcon className="size-4 text-ok" />
                        ) : standing === "skipped" ? (
                          <CheckIcon className="size-4 text-faint" />
                        ) : standing === "current" ? (
                          <Spinner className="size-3.5 text-primary" />
                        ) : (
                          <span
                            className={cn(
                              "size-1.5 rounded-full",
                              standing === "stopped" ? "bg-bad" : "bg-line-strong",
                            )}
                          />
                        )}
                      </span>
                      <span className={cn(live && "font-semibold")}>
                        {standing === "done" || standing === "skipped"
                          ? finishedLine(data, stage)
                          : stage.label}
                      </span>
                    </div>
                    {live ? (
                      <div className="flex flex-col gap-2 pl-8">
                        {count ? (
                          <>
                            <ProgressBar label={stage.label} done={count.read} total={count.total} />
                            <div className="flex justify-between text-xs text-muted-foreground">
                              <span>
                                {count.read.toLocaleString()} of {count.total.toLocaleString()}
                              </span>
                              <span>{left !== null ? leftText(left) : ""}</span>
                            </div>
                          </>
                        ) : indexProgress ? (
                          <span className="text-xs text-muted-foreground">{indexProgress.message}</span>
                        ) : (
                          <span
                            className={cn(
                              "text-xs",
                              standing === "stopped" ? "text-bad" : "text-muted-foreground",
                            )}
                          >
                            {data.message}
                          </span>
                        )}
                        {standing === "stopped" ? (
                          <div className="flex flex-col gap-2">
                            {data.note ? <Note>{data.note}</Note> : null}
                            <Button
                              variant="primary"
                              className="self-start"
                              disabled={retrying}
                              onClick={retry}
                            >
                              {retrying ? (
                                <>
                                  <Spinner /> Starting…
                                </>
                              ) : (
                                "Try again"
                              )}
                            </Button>
                            <Note>The log has the details: {data.log_path}</Note>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                )
              })}
            </ol>
          ) : (
            <Note>
              <span className="inline-flex items-center gap-2">
                <Spinner />{" "}
                {data ? data.message : startedAt === undefined ? "Reading your progress…" : "Starting…"}
              </span>
            </Note>
          )}
          {error ? <Note bad>{error}</Note> : null}
        </>
      }
      right={
        // The right column fills in as the stages finish: the page picture while the connections
        // are read, then the first look the moment people are in.
        known > 0 || ready ? (
          <FirstLookPanel look={look.data ?? { people: [], total: 0, questions: [] }} ready={ready} />
        ) : data && (count !== null || processing || data.step === "linkedin") ? (
          <>
            <h2 className="m-0 text-[15px] font-semibold">Your connections page, as I read it</h2>
            <PageShot
              url="/api/install/linkedin-page"
              address="linkedin.com/mynetwork/invite-connect/connections"
              reading={count !== null}
              caption={
                count
                  ? `${count.read.toLocaleString()} of ${count.total.toLocaleString()} read so far`
                  : "Read. Building your network from it now."
              }
            />
          </>
        ) : (
          <Info
            title="While you wait"
            lines={[
              "Your first look: a few people you know and three questions to try",
              "Closing the window does not stop anything",
              "The whole network takes a while; you can use Powerpacks meanwhile",
            ]}
          />
        )
      }
    />
  )
}
