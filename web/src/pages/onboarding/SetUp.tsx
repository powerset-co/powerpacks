import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useLocation, useNavigate, useParams } from "react-router-dom"

import { CheckIcon, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { HOME } from "@/lib/nav"
import { continueSetup, fetchFirstLook, fetchInstall } from "@/lib/api/install"
import { cn, isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

import MAPPING from "../../../../packs/powerset/primitives/install/setup-steps-mapping.json"

import { Frame, Info, Note } from "./Frame"
import { stopDetail } from "@/pages/install/sources"

import { leftText, secondsLeft, type Sample } from "./pace"
import { FirstLookPanel } from "./FirstLookPanel"
import { PageShot } from "./PageShot"

const POLL_MS = 1_500
const PEOPLE_POLL_MS = 5_000
const KEY = ["onboarding", "install"] as const
const DONE = new Set(["completed", "skipped"])

type Row = (typeof MAPPING.rows)[number]
type Standing = "done" | "current" | "stopped" | "next"

interface Progress {
  data: InstallStatus
  samples: Sample[]
}

/** The running step's count from its event's details: {read, total} from the LinkedIn read,
 *  {done, total} from Modal and the processing stages. */
function counts(data: InstallStatus | undefined): { read: number; total: number } | null {
  const details = data?.action?.details
  if (!isRecord(details)) return null
  const read = details.read ?? details.done
  const { total } = details
  return typeof read === "number" && typeof total === "number" && total > 0 ? { read, total } : null
}

/** Modal's own estimate of the seconds left, and when the record carrying it was written. */
function estimateOf(data: InstallStatus | undefined): { seconds: number; at: number } | null {
  const details = data?.action?.details
  if (!data || !isRecord(details) || typeof details.left_seconds !== "number") return null
  return { seconds: details.left_seconds, at: Date.parse(data.updated_at) }
}

/** The row's steps this run plans. */
function planned(data: InstallStatus, row: Row): string[] {
  return row.steps.filter((step) => data.plan?.includes(step))
}

/** Where a row stands: done once every planned step is completed or skipped, current while the
 *  run is on one of its steps (stopped if that step failed), next otherwise. */
function standingOf(data: InstallStatus, row: Row): Standing {
  if (planned(data, row).every((step) => DONE.has(data.steps[step]?.status ?? ""))) return "done"
  if (row.steps.includes(data.step)) return data.status === "failed" ? "stopped" : "current"
  return "next"
}

/** A finished row says the record's line for its last planned step. */
function finishedLine(data: InstallStatus, row: Row): string {
  const last = planned(data, row).at(-1)
  return last ? (data.steps[last]?.message ?? row.label) : row.label
}

/** The time now (ms), renewed every second while `ticking`, so an estimate counts down. */
function useNow(ticking: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!ticking) return
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [ticking])
  return now
}

/** A stop's way out: the record's note, Try again, the detail behind the stop, where the logs are. */
function Stopped({ data, retrying, retry }: { data: InstallStatus; retrying: boolean; retry: () => void }) {
  return (
    <div className="flex flex-col gap-2">
      {data.note ? <Note>{data.note}</Note> : null}
      <Button variant="primary" className="self-start" disabled={retrying} onClick={retry}>
        {retrying ? (
          <>
            <Spinner /> Starting…
          </>
        ) : (
          "Try again"
        )}
      </Button>
      {stopDetail(data) ? <Note bad>{stopDetail(data)}</Note> : null}
      <Note>Logs are in {data.log_path.replace(/\/[^/]*$/, "/")}</Note>
    </div>
  )
}

/** Set up: the run LinkedIn's Continue started (or Sources', for Import), in the background. This
 *  screen only shows its record as a checklist of the golden rows the run plans; the one thing it
 *  starts is Try again after a stop. The first look opens once people are in. */
export function SetUp() {
  const client = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  // Try again after a stop; the spinner stays until the record shows the run.
  const [retrying, setRetrying] = useState(false)
  // The record, with the count sampled at every read: the pace so far gives the time left when
  // the step has no estimate of its own.
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
  // The same screen serves Set up (the LinkedIn run) and Import (the run over every source).
  const step = useParams().step === "import" ? "import" : "setup"
  const state: unknown = useLocation().state
  const startedAt = isRecord(state) && typeof state.startedAt === "number" ? state.startedAt : undefined
  const record = install.data?.data
  const data =
    record && startedAt !== undefined && Date.parse(record.updated_at) < startedAt ? undefined : record
  const failed = data?.status === "failed"
  const navigate = useNavigate()
  const ready = data?.event === "search.ready"
  const count = counts(data)
  // The page picture's count is the LinkedIn read's alone, not a later step's.
  const read = data?.step === "linkedin" ? count : null
  // Modal's estimate counts down from when it was written; the LinkedIn read's comes from its pace.
  const estimate = estimateOf(data)
  const now = useNow(estimate !== null)
  const modalLeft = estimate
    ? Math.min(estimate.seconds, estimate.seconds - (now - estimate.at) / 1000)
    : null
  // A Modal estimate that has run out reads "under a minute left" until the next write, not nothing.
  const left =
    modalLeft !== null
      ? Math.max(modalLeft, 1)
      : count
        ? secondsLeft(install.data?.samples ?? [], count.total)
        : null
  const processing = data !== undefined && data.step !== "linkedin" && !ready

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
    // The app runs again the run the record names (bin/linkedin-setup or bin/onboard).
    continueSetup({}).catch((caught: unknown) => {
      setError(errorText(caught))
      setRetrying(false)
    })
  }

  // The rows this run plans; a row with `needs` only when that step is planned.
  const rows = data
    ? MAPPING.rows.filter(
        (row) =>
          planned(data, row).length > 0 && (row.needs === "" || data.plan?.includes(row.needs) === true),
      )
    : []
  const current = data ? rows.find((row) => row.steps.includes(data.step)) : undefined
  return (
    <Frame
      step={step}
      left={
        <>
          <h1 className="m-0 text-[22px] font-bold leading-tight">
            {failed ? "Setup stopped" : ready ? "Search is ready" : (current?.label ?? "Setting up")}
          </h1>
          {install.error ? <Note bad>{errorText(install.error)}</Note> : null}
          {data && rows.length > 0 ? (
            <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Setup stages">
              {rows.map((row) => {
                const standing = standingOf(data, row)
                const live = standing === "current" || standing === "stopped"
                return (
                  <li key={row.label} className="flex flex-col gap-2" data-stage={standing}>
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
                        {standing === "done" ? finishedLine(data, row) : row.label}
                      </span>
                    </div>
                    {live ? (
                      <div className="flex flex-col gap-2 pl-8">
                        <div className="flex justify-between gap-3 text-xs">
                          <span className={standing === "stopped" ? "text-bad" : "text-muted-foreground"}>
                            {data.message}
                          </span>
                          <span className="shrink-0 text-muted-foreground">
                            {left !== null ? leftText(left) : ""}
                          </span>
                        </div>
                        {standing === "stopped" ? (
                          <Stopped data={data} retrying={retrying} retry={retry} />
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                )
              })}
            </ol>
          ) : null}
          {data && failed && current === undefined ? (
            // Stopped on a step no row lists: the stop itself, with its way out.
            <div className="flex flex-col gap-2">
              <span className="text-xs text-bad">{data.message}</span>
              <Stopped data={data} retrying={retrying} retry={retry} />
            </div>
          ) : null}
          {!data || (rows.length === 0 && !failed) ? (
            <Note>
              <span className="inline-flex items-center gap-2">
                <Spinner />{" "}
                {data ? data.message : startedAt === undefined ? "Reading your progress…" : "Starting…"}
              </span>
            </Note>
          ) : null}
          {error ? <Note bad>{error}</Note> : null}
          {data?.plan?.includes("deep_context") && processing ? (
            <div className="mt-2 flex flex-col gap-2">
              <Button variant="primary" className="self-start" onClick={() => void navigate(HOME.href)}>
                Start using Powerpacks now
              </Button>
              <Note>
                The import keeps running in the background; Setup in the side nav shows where it is.
                {left !== null ? ` This stage: ${leftText(left)}.` : ""}
              </Note>
            </div>
          ) : null}
        </>
      }
      right={
        // The right column fills in as the stages finish: the page picture while the connections
        // are read, then the first look the moment people are in.
        known > 0 || ready ? (
          <FirstLookPanel
            look={look.data ?? { people: [], total: 0, questions: [] }}
            ready={ready}
            sourcesNext={false}
          />
        ) : data && (processing || data.step === "linkedin") ? (
          <>
            <h2 className="m-0 text-[15px] font-semibold">Your connections page, as I read it</h2>
            <PageShot
              url="/api/install/linkedin-page"
              address="linkedin.com/mynetwork/invite-connect/connections"
              reading={read !== null}
              caption={
                read
                  ? `${read.read.toLocaleString()} of ${read.total.toLocaleString()} read so far`
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
