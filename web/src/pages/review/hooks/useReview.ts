import { createContext, useContext } from "react"

import type { DecisionProgress, PageProgress, ReviewView } from "@/types/review"

/** What the page gives the stage on screen: the screen's settings and the page-wide actions. */
export interface Review {
  /** The counts as they stand: the page load's, then each applied click response's. */
  progress: PageProgress
  /** The user opened this screen deliberately (`preview=1`); tab links keep it. */
  preview: boolean
  /** The queue carousel (`debug=1`). */
  debug: boolean
  /** The queue position the URL asked for (`index`), 0 without one. */
  index: number
  /** A card's fade-out wait: 50 ms, 0 under reduced motion. */
  fadeMs: number
  /** A message for 1.8 s; a new one replaces the old. */
  toast: (message: string) => void
  /** An error for 6 s. */
  toastError: (message: string) => void
  /** Repaints the step badges and the tab counts from a click response; no refetch. */
  applyProgress: (progress: DecisionProgress) => void
  /** The stage check: the stage becomes a check mark over `message` for 650 ms, then
   *  `stage`'s screen loads. Marks a stage-complete action in flight until it does. */
  transition: (message: string, stage: ReviewView) => void
  /** Reads this screen again from the server; the stage remounts on the answer. */
  reload: () => void
  /** Says `message`, fades the stage out, then reloads. */
  leaveAndReload: (message: string) => void
  /** Re-reads the server's status now (Enrich and Done only; a no-op elsewhere). */
  syncStatus: () => void
  /** Tells the status watcher the server is at `stage` (this screen put it there), so only a
   *  later change counts as observed. */
  noteServerStage: (stage: ReviewView) => void
  /** A guidance draft is typed (or no longer): the status watcher never moves the screen under one. */
  setGuidanceDraft: (typed: boolean) => void
  /** A stage-complete or approve POST is in flight (or no longer): the status watcher waits. */
  setCompleting: (inFlight: boolean) => void
}

export const ReviewContext = createContext<Review | null>(null)

/** The page's actions and screen settings, for a stage or a shared review component. */
export function useReview(): Review {
  const review = useContext(ReviewContext)
  if (!review) throw new Error("useReview needs the review page around it")
  return review
}
