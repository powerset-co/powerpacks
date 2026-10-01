import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"
import { STAGE_DONE } from "@/lib/review/copy"
import { stageHref } from "@/lib/review/links"
import type { EnrichmentJob } from "@/lib/review/sync"
import { fadeMs, STAGE_CHECK_MS } from "@/lib/review/timing"
import type { DecisionProgress, PageProgress, ReviewView } from "@/types/review"

import type { Review } from "./useReview"
import type { ReviewToast } from "./useReviewToast"
import type { Screen } from "./useScreen"
import { useServerWatch } from "./useServerWatch"

interface ScreenReviewOptions {
  screen: Screen
  /** The Enrich panel is what the stage shows (not the synthesis handoff). */
  panelShown: boolean
  toast: ReviewToast
  reload: () => void
  open: (href: string) => void
}

/**
 * One screen's state and the `Review` its stage works through: the live counts, the stage
 * check, the leave-and-reload fade, and the server watch. Mounted per screen (the caller is
 * keyed by `screen.id`), so all of it starts over when the next screen loads.
 */
export function useScreenReview({ screen, panelShown, toast, reload, open }: ScreenReviewOptions) {
  const { page, preview, debug, index } = screen
  const reducedMotion = useReducedMotion()
  const fade = fadeMs(reducedMotion)

  /** The counts click responses carried, laid over the page load's; a response names the
   *  counts its click could change (a LinkedIn decision only its own). */
  const [applied, setApplied] = useState<Partial<DecisionProgress>>({})
  const applyProgress = useCallback(
    (counts: Partial<DecisionProgress>) => setApplied((before) => ({ ...before, ...counts })),
    [],
  )
  const progress: PageProgress = useMemo(() => ({ ...page.progress, ...applied }), [page.progress, applied])

  /** The stage check's words while a stage transition runs; null otherwise. */
  const [check, setCheck] = useState<string | null>(null)
  const [leaving, setLeaving] = useState(false)
  const [job, setJob] = useState<EnrichmentJob | null>(null)
  const completing = useRef(false)
  const draft = useRef(false)
  const timers = useRef<number[]>([])
  // A late answer to a screen that has gone must not move the screen opened since.
  const gone = useRef(false)
  useEffect(() => {
    const pending = timers.current
    gone.current = false
    return () => {
      gone.current = true
      pending.forEach((timer) => window.clearTimeout(timer))
    }
  }, [])

  const transition = useCallback(
    (message: string, stage: ReviewView) => {
      if (gone.current) return
      completing.current = true
      setCheck(message)
      timers.current.push(window.setTimeout(() => open(stageHref(stage)), STAGE_CHECK_MS))
    },
    [open],
  )

  const { say } = toast
  const leaveAndReload = useCallback(
    (message: string) => {
      if (gone.current) return
      say(message)
      setLeaving(true)
      timers.current.push(window.setTimeout(reload, fade))
    },
    [say, reload, fade],
  )

  const onForward = useCallback((stage: ReviewView) => transition(STAGE_DONE.enrich, stage), [transition])
  const { syncStatus, noteServerStage } = useServerWatch({
    page,
    preview,
    panelShown,
    completing,
    draft,
    onForward,
    onStale: reload,
    onJob: setJob,
  })

  const setGuidanceDraft = useCallback((typed: boolean) => {
    draft.current = typed
  }, [])
  const setCompleting = useCallback((inFlight: boolean) => {
    completing.current = inFlight
  }, [])

  const { sayError } = toast
  const review: Review = useMemo(
    () => ({
      progress,
      preview,
      debug,
      index,
      fadeMs: fade,
      toast: say,
      toastError: sayError,
      applyProgress,
      transition,
      reload,
      leaveAndReload,
      syncStatus,
      noteServerStage,
      setGuidanceDraft,
      setCompleting,
    }),
    [
      progress,
      preview,
      debug,
      index,
      fade,
      say,
      sayError,
      applyProgress,
      transition,
      reload,
      leaveAndReload,
      syncStatus,
      noteServerStage,
      setGuidanceDraft,
      setCompleting,
    ],
  )

  return { review, progress, check, leaving, job }
}
