import { useCallback, useEffect, useRef, type MutableRefObject } from "react"

import { fetchStatus, watchEvents } from "@/lib/api/review"
import { decideStatus, runningJob, type EnrichmentJob } from "@/lib/review/sync"
import type { ReviewPage, ReviewStatus, ReviewView } from "@/types/review"

interface ServerWatch {
  page: ReviewPage
  preview: boolean
  /** The Enrich panel is on screen to take a running job's numbers. */
  panelShown: boolean
  /** A stage-complete action is in flight: the watcher waits. */
  completing: MutableRefObject<boolean>
  /** A guidance draft is typed: the watcher never moves the screen. */
  draft: MutableRefObject<boolean>
  /** The server moved ahead while this screen was open. */
  onForward: (stage: ReviewView) => void
  /** The server's state changed under this screen. */
  onStale: () => void
  /** A running enrichment's numbers. */
  onJob: (job: EnrichmentJob) => void
}

/**
 * One screen's watch on the server. Only a screen with `external_updates` (Enrich, Done)
 * reads the status once on arrival, listens to /api/events, and re-reads the status on every
 * connect and on every message that is not a running job. Worth and LinkedIn never open the
 * stream or read the status. Mount it once per screen: what it has seen resets with the screen.
 */
export function useServerWatch(watch: ServerWatch) {
  const latest = useRef(watch)
  useEffect(() => {
    latest.current = watch
  })
  const watches = watch.page.external_updates
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
    const { page, preview, draft, onForward, onStale } = latest.current
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
      (event) => {
        const job = runningJob(event, latest.current.panelShown)
        if (job) latest.current.onJob(job)
        else void syncStatus()
      },
      () => void syncStatus(),
    )
    return () => {
      alive.current = false
      close()
    }
  }, [watches, syncStatus])

  const noteServerStage = useCallback((stage: ReviewView) => {
    lastStage.current = stage
  }, [])

  return { syncStatus: useCallback(() => void syncStatus(), [syncStatus]), noteServerStage }
}
