import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { useReducedMotion } from "@/hooks/useReducedMotion"
import { fadeMs } from "@/lib/review/timing"
import type { DecisionProgress, PageProgress } from "@/types/review"

import type { Review } from "./useReview"
import type { ReviewToast } from "./useReviewToast"
import type { Screen } from "./useScreen"

interface ScreenReviewOptions {
  screen: Screen
  toast: ReviewToast
  reload: () => void
}

// The review the Check LinkedIn stage works through: the screen's settings, the live count,
// the toast, and leave-and-reload (the fade, then the screen is read again).
export function useScreenReview({ screen, toast, reload }: ScreenReviewOptions) {
  const { page, preview, debug, index } = screen
  const reducedMotion = useReducedMotion()
  const fade = fadeMs(reducedMotion)
  /** The counts click responses carried, laid over the page load's. */
  const [applied, setApplied] = useState<Partial<DecisionProgress>>({})
  const applyProgress = useCallback(
    (counts: Partial<DecisionProgress>) => setApplied((before) => ({ ...before, ...counts })),
    [],
  )
  const progress: PageProgress = useMemo(() => ({ ...page.progress, ...applied }), [page.progress, applied])
  const [leaving, setLeaving] = useState(false)
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
  const { say, sayError } = toast
  const leaveAndReload = useCallback(
    (message: string) => {
      if (gone.current) return
      say(message)
      setLeaving(true)
      timers.current.push(window.setTimeout(reload, fade))
    },
    [say, reload, fade],
  )
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
      reload,
      leaveAndReload,
    }),
    [progress, preview, debug, index, fade, say, sayError, applyProgress, reload, leaveAndReload],
  )
  return { review, progress, leaving }
}
