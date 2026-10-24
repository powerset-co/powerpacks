import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"

import { CheckIcon, ProgressBar, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, fetchInstall } from "@/lib/api/install"
import { fetchPeople } from "@/lib/api/people"
import { cn, isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

import { Frame, Info, Note } from "./Frame"
import { leftText, secondsLeft, type Sample } from "./pace"
import { PageShot } from "./PageShot"
import { after } from "./steps"

const POLL_MS = 1_500
const PEOPLE_POLL_MS = 5_000
const KEY = ["onboarding", "install"] as const

// The stages setup goes through after LinkedIn is connected (steps.py), as the person sees them.
const STAGES = [
  { steps: ["linkedin"], label: "Reading your connections", done: "Connections read" },
  { steps: ["deep_context"], label: "Learning who they are", done: "Learned who they are" },
  { steps: ["enrich"], label: "Enriching people", done: "People enriched" },
  { steps: ["index", "validate"], label: "Building your search index", done: "Search index built" },
  { steps: ["ready"], label: "Search is ready", done: "Search is ready" },
] as const
const DONE = new Set(["completed", "skipped"])

interface Progress {
  data: InstallStatus
  samples: Sample[]
}

/** The LinkedIn count's numbers, from the event's details (workflow.py linkedin.reading.count). */
function counts(data: InstallStatus | undefined): { read: number; total: number } | null {
  const details = data?.action?.details
  if (data?.step !== "linkedin" || !isRecord(details)) return null
  const { read, total } = details
  return typeof read === "number" && typeof total === "number" && total > 0 ? { read, total } : null
}

type Standing = "done" | "current" | "stopped" | "next"

/** Where one stage stands: done, the one under way, stopped there, or still to come. */
function standingOf(data: InstallStatus, stage: (typeof STAGES)[number]): Standing {
  const current = stage.steps.some((step) => step === data.step)
  if (current) {
    if (data.status === "failed") return "stopped"
    if (data.event === "search.ready" || (stage.steps[0] === "ready" && DONE.has(data.status))) return "done"
    return "current"
  }
  const states = stage.steps.map((step) => data.steps[step]?.status)
  return states.every((state) => DONE.has(state ?? "")) ? "done" : "next"
}

/** Set up: setup runs in the background (bin/onboard) from the moment this opens; the stages
 *  show as they go, the LinkedIn count with the time left, the index with Modal's progress. The
 *  first look opens once people are in. */
export function SetUp() {
  const navigate = useNavigate()
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
  const data = install.data?.data
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

  // People appear during processing; once there are some the first look is ready.
  const people = useQuery({
    queryKey: ["people"],
    queryFn: fetchPeople,
    enabled: processing,
    refetchInterval: processing ? PEOPLE_POLL_MS : false,
    retry: false,
  })
  const known = people.data?.length ?? 0
  const next = after("setup")
  useEffect(() => {
    if (ready && next) void navigate(next.href)
  }, [ready, next, navigate])

  const retry = () => {
    setRetrying(true)
    setError(null)
    continueSetup({}).catch((caught: unknown) => {
      setError(errorText(caught))
      setRetrying(false)
    })
  }

  const current = data ? STAGES.find((stage) => standingOf(data, stage) === "current") : undefined
  const indexProgress = data?.step === "index" ? data.index_progress : null
  return (
    <Frame
      step="setup"
      left={
        <>
          <h1 className="m-0 text-[22px] font-bold leading-tight">
            {failed ? "Setup stopped" : (current?.label ?? "Setting up")}
          </h1>
          {install.error ? <Note bad>{errorText(install.error)}</Note> : null}
          {data ? (
            <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Setup stages">
              {STAGES.map((stage) => {
                const standing = standingOf(data, stage)
                const live = standing === "current" || standing === "stopped"
                return (
                  <li key={stage.label} className="flex flex-col gap-2" data-stage={standing}>
                    <div
                      className={cn(
                        "flex items-center gap-3 text-[13px] transition-colors duration-med",
                        standing === "next" ? "text-faint" : "text-foreground",
                      )}
                    >
                      <span className="grid size-5 shrink-0 place-items-center" aria-hidden>
                        {standing === "done" ? (
                          <CheckIcon className="size-4 text-ok" />
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
                        {standing === "done" ? stage.done : stage.label}
                      </span>
                    </div>
                    {live ? (
                      <div className="flex flex-col gap-2 pl-8">
                        {count ? (
                          <>
                            <ProgressBar label="Connections read" done={count.read} total={count.total} />
                            <div className="flex justify-between text-xs text-muted-foreground">
                              <span>
                                {count.read.toLocaleString()} of {count.total.toLocaleString()} connections
                              </span>
                              <span>{left !== null ? leftText(left) : ""}</span>
                            </div>
                          </>
                        ) : indexProgress ? (
                          <>
                            <ProgressBar
                              label="Search index"
                              done={Math.round((indexProgress.progress ?? 0) * 100)}
                              total={indexProgress.progress === undefined ? 0 : 100}
                            />
                            <span className="text-xs text-muted-foreground">{indexProgress.message}</span>
                          </>
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
                <Spinner /> Reading your progress…
              </span>
            </Note>
          )}
          {known > 0 && next && !ready ? (
            <Button variant="primary" size="lg" className="rise-in" onClick={() => void navigate(next.href)}>
              See your first look ({known.toLocaleString()} people so far)
            </Button>
          ) : null}
          {error ? <Note bad>{error}</Note> : null}
        </>
      }
      right={
        data && !ready && (count !== null || processing || data.step === "linkedin") ? (
          <>
            <h2 className="m-0 text-[15px] font-semibold">Your connections page, as I read it</h2>
            <PageShot
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
