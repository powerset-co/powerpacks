// The setup record a screen shows: read every POLL_MS, kept only when it is the screen's own run
// (Set up's LinkedIn run, Import's run over every source) and newer than the click that opened it.

import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useLocation } from "react-router-dom"

import { errorText } from "@/lib/api/http"
import { continueSetup, fetchInstall } from "@/lib/api/install"
import { isRecord } from "@/lib/utils"
import type { InstallStatus } from "@/types/install"

import { secondsLeft, type Sample } from "./pace"

const POLL_MS = 1_500
const KEY = ["onboarding", "install"] as const

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

const MESSAGE_IMPORTS = ["gmail_import", "imessage_import", "whatsapp_import"]

/** Whether the run imports messages: Import's run (Sources' Start), never Set up's (LinkedIn). */
export function importsMessages(data: InstallStatus): boolean {
  return MESSAGE_IMPORTS.some((step) => data.plan?.includes(step))
}

export interface Run {
  data: InstallStatus | undefined
  /** The running step's count, for the screens' own pictures. */
  count: { read: number; total: number } | null
  /** Seconds left in the running step: Modal's estimate, else the pace so far. */
  left: number | null
  ready: boolean
  failed: boolean
  /** Opened by Start or Continue: no record yet from the run that click started. */
  starting: boolean
  error: unknown
  /** Try again after a stop; `retrying` until the record shows the run, `retryError` if it could not start. */
  retry: () => void
  retrying: boolean
  retryError: string | null
}

/** The setup record when it is this screen's run (`mine`) and newer than the click that opened
 *  the screen; the record is the last run's until the new one writes. */
export function useRun(mine: (data: InstallStatus) => boolean): Run {
  const client = useQueryClient()
  const [retryError, setRetryError] = useState<string | null>(null)
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
  const state: unknown = useLocation().state
  const startedAt = isRecord(state) && typeof state.startedAt === "number" ? state.startedAt : undefined
  const record = install.data?.data
  const data =
    record && mine(record) && (startedAt === undefined || Date.parse(record.updated_at) >= startedAt)
      ? record
      : undefined
  const count = counts(data)
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
  const retry = () => {
    setRetrying(true)
    setRetryError(null)
    // The app runs again the run the record names (bin/linkedin-setup or bin/onboard).
    continueSetup({}).catch((caught: unknown) => {
      setRetryError(errorText(caught))
      setRetrying(false)
    })
  }
  return {
    data,
    count,
    left,
    ready: data?.event === "search.ready",
    failed: data?.status === "failed",
    starting: data === undefined && startedAt !== undefined,
    error: install.error,
    retry,
    retrying,
    retryError,
  }
}
