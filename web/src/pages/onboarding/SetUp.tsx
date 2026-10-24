import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"

import { ProgressBar, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { errorText } from "@/lib/api/http"
import { continueSetup, fetchInstall } from "@/lib/api/install"
import { fetchPeople } from "@/lib/api/people"
import { isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

import { Frame, Heading, Info, Note } from "./Frame"
import { leftText, secondsLeft, type Sample } from "./pace"
import { PageShot } from "./PageShot"
import { after } from "./steps"

const POLL_MS = 1_500
const KEY = ["onboarding", "install"] as const

interface Progress {
  data: InstallStatus
  samples: Sample[]
}
const PEOPLE_POLL_MS = 5_000
// The steps after the sources' syncs (steps.py PROCESSING_STEPS): people appear during them.
const PROCESSING = new Set(["deep_context", "enrich", "index", "validate", "ready"])

/** The LinkedIn count's numbers, from the event's details (workflow.py linkedin.reading.count). */
function counts(data: InstallStatus | undefined): { read: number; total: number } | null {
  const details = data?.action?.details
  if (data?.step !== "linkedin" || !isRecord(details)) return null
  const { read, total } = details
  return typeof read === "number" && typeof total === "number" && total > 0 ? { read, total } : null
}

/** Set up: setup runs in the background (bin/onboard) while the count and the time left show; the
 *  first look opens once people are in. */
export function SetUp() {
  const navigate = useNavigate()
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The record, with the LinkedIn count sampled at every read: the pace so far gives the time left.
  const client = useQueryClient()
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
  const processing = data !== undefined && PROCESSING.has(data.step)
  const ready = data?.event === "search.ready"
  // Setup can be started when nothing runs: before it (setup.ready), paused, or stopped.
  const startable = data !== undefined && !running && !ready

  // The LinkedIn count, sampled at every poll: the pace so far gives the time left.
  const count = counts(data)
  const left = count ? secondsLeft(install.data?.samples ?? [], count.total) : null

  // People appear during processing; once there are some the first look is ready.
  const people = useQuery({
    queryKey: ["people"],
    queryFn: fetchPeople,
    enabled: processing,
    refetchInterval: processing && !ready ? PEOPLE_POLL_MS : false,
    retry: false,
  })
  const known = people.data?.length ?? 0
  const next = after("setup")
  useEffect(() => {
    if (ready && next) void navigate(next.href)
  }, [ready, next, navigate])

  // The spinner stays after the call answers: the button leaves once the record shows the run.
  const start = () => {
    setStarting(true)
    setError(null)
    continueSetup({}).catch((caught: unknown) => {
      setError(errorText(caught))
      setStarting(false)
    })
  }

  const title = failed
    ? "Setup stopped"
    : count
      ? "Reading your connections"
      : processing
        ? "Building your network"
        : running
          ? "Setting up"
          : "Ready to set up"
  return (
    <Frame
      step="setup"
      left={
        <>
          <Heading
            title={title}
            line={
              failed
                ? data.message
                : startable
                  ? starting
                    ? "Starting the read of your LinkedIn connections…"
                    : "Read your LinkedIn connections and build your network from them."
                  : undefined
            }
          />
          {install.error ? <Note bad>{errorText(install.error)}</Note> : null}
          {startable ? (
            <Button variant="primary" size="lg" disabled={starting} onClick={start}>
              {starting ? (
                <>
                  <Spinner /> Starting…
                </>
              ) : failed ? (
                "Try again"
              ) : (
                "Set up"
              )}
            </Button>
          ) : null}
          {count ? (
            <div className="flex flex-col gap-2">
              <ProgressBar label="Connections read" done={count.read} total={count.total} />
              <div className="flex justify-between text-[13px]">
                <span>
                  {count.read.toLocaleString()} of {count.total.toLocaleString()} connections
                </span>
                <span>{left !== null ? leftText(left) : ""}</span>
              </div>
            </div>
          ) : data && !startable ? (
            <Note>
              <span className="inline-flex items-center gap-2">
                {running ? <Spinner /> : null}
                {data.message}
              </span>
            </Note>
          ) : null}
          {data?.note && (!startable || failed) ? <Note>{data.note}</Note> : null}
          {failed ? <Note>The log has the details: {data.log_path}</Note> : null}
          {known > 0 && next ? (
            <Button variant="primary" size="lg" onClick={() => void navigate(next.href)}>
              See your first look ({known.toLocaleString()} people so far)
            </Button>
          ) : null}
          {error ? <Note bad>{error}</Note> : null}
          {count ? <Note>Estimate from the pace so far; it updates as LinkedIn answers.</Note> : null}
        </>
      }
      right={
        // Once the LinkedIn read is under way the page it reads takes the column; before it,
        // and once setup is over, what to expect.
        data && (count !== null || processing || data.step === "linkedin") && !ready ? (
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
              "The whole network takes a few hours; you can use Powerpacks meanwhile",
            ]}
          />
        )
      }
    />
  )
}
