import { createContext, useContext } from "react"

import type { DecisionProgress, PageProgress } from "@/types/review"

/** What the page gives the stage on screen: the screen's settings and the page-wide actions. */
export interface Review {
  /** The counts as they stand: the page load's, then each applied click response's. */
  progress: PageProgress
  /** The user opened this screen deliberately (`preview=1`). */
  preview: boolean
  /** The queue carousel (`debug=1`). */
  debug: boolean
  /** The queue position the URL asked for (`index`), 0 without one. */
  index: number
  /** A card's fade-out wait: 150 ms, 0 under reduced motion. */
  fadeMs: number
  /** A message for 1.8 s; a new one replaces the old. */
  toast: (message: string) => void
  /** An error for 6 s. */
  toastError: (message: string) => void
  /** Repaints the count from a click response; no refetch. */
  applyProgress: (progress: Partial<DecisionProgress>) => void
  /** Reads this screen again from the server; the stage remounts on the answer. */
  reload: () => void
  /** Says `message`, fades the stage out, then reloads. */
  leaveAndReload: (message: string) => void
}

export const ReviewContext = createContext<Review | null>(null)

/** The page's actions and screen settings, for a stage or a shared review component. */
export function useReview(): Review {
  const review = useContext(ReviewContext)
  if (!review) throw new Error("useReview needs the review page around it")
  return review
}
