import { useCallback, useEffect, useRef, type MutableRefObject } from "react"

import { fetchStatus, watchEvents } from "@/lib/api/review"
import { decideStatus } from "@/lib/review/sync"
import { STATUS_POLL_MS } from "@/lib/review/timing"
import type { ReviewPage, ReviewStatus, ReviewView } from "@/types/review"

interface ServerWatch {
  page: ReviewPage
  preview: boolean
  /** A stage-complete action is in flight: the watcher waits. */
  completing: MutableRefObject<boolean>
  /** A guidance draft is typed: the watcher never moves the screen. */
  draft: MutableRefObject<boolean>
  /** The server moved ahead while this screen was open. */
  onForward: (stage: ReviewView) => void
  /** The server's state changed under this screen. */
  onStale: () => void
  /** Every status read, as it arrives. */
  onStatus: (status: ReviewStatus) => void
}

/**
 * One screen's watch on the server. Only a screen with `external_updates` (Enrich, Done)
 * reads the status once on arrival, listens to /api/events, and re-reads the status on every
 * connect and every message. Enrich also re-reads it every few seconds: the agent enriches from
 * its own process, which this server's stream never hears. Worth and LinkedIn never open the
 * stream or read the status. Mount it once per screen: what it has seen resets with the screen.
 */
export function useServerWatch(watch: ServerWatch) {
  const latest = useRef(watch)
  useEffect(() => {
    latest.current = watch
  })
  const watches = watch.page.external_updates
  /** The Enrich screen waits on work done outside this server. */
  const waits = watch.page.view === "enrich"
  const lastStage = useRef<ReviewView | "">("")
  const alive = useRef(true)

  const syncStatus = useCallback(async () => {
    const completing = () => latest.current.completing.current
    if (!watches || completing()) return
    let status: ReviewStatus
    try {
      status = await fetchStatus()
    } catch {
      // The server may be restarting; the next event or connect reads again.
      return
    }
    // The screen left, or a stage-complete click landed, while the status was being read.
    if (!alive.current || completing()) return
    const { page, preview, draft, onForward, onStale, onStatus } = latest.current
    onStatus(status)
    const decision = decideStatus({
      view: page.view,
      preview,
      hasDraft: draft.current,
      lastStage: lastStage.current,
      status,
      loadedToken: page.state_token,
    })
    lastStage.current = decision.lastStage
    if (decision.action.kind === "navigate") onForward(decision.action.stage)
    if (decision.action.kind === "reload") onStale()
  }, [watches])

  useEffect(() => {
    alive.current = true
    if (!watches) return
    void syncStatus()
    const close = watchEvents(
      () => void syncStatus(),
      () => void syncStatus(),
    )
    const poll = waits ? window.setInterval(() => void syncStatus(), STATUS_POLL_MS) : null
    return () => {
      alive.current = false
      close()
      if (poll !== null) window.clearInterval(poll)
    }
  }, [watches, waits, syncStatus])

  const noteServerStage = useCallback((stage: ReviewView) => {
    lastStage.current = stage
  }, [])

  return { syncStatus: useCallback(() => void syncStatus(), [syncStatus]), noteServerStage }
}
