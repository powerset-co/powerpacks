// What a screen that watches the server does with each status read (/api/status). Only Enrich
// and Done watch.

import type { ReviewStatus, ReviewView } from "@/types/review"

const STAGE_ORDER: readonly ReviewView[] = ["worth", "enrich", "linkedin", "done"]

export interface StatusInput {
  /** The screen on show. */
  view: ReviewView
  /** Opened deliberately (`preview=1`): never moved. */
  preview: boolean
  /** A guidance draft is typed: never yanked. */
  hasDraft: boolean
  /** The server stage the previous status read on this screen showed; "" before the first. */
  lastStage: ReviewView | ""
  status: ReviewStatus
  /** The `state_token` the screen loaded with. */
  loadedToken: string
}

export type StatusAction =
  /** The stage check, then that stage's screen. */
  | { kind: "navigate"; stage: ReviewView }
  /** Read the screen's data again. */
  | { kind: "reload" }
  | { kind: "nothing" }

export interface StatusDecision {
  action: StatusAction
  /** What the next read compares against. */
  lastStage: ReviewView
}

const NOTHING: StatusAction = { kind: "nothing" }

/**
 * Feed-forward: the screen only ever moves FORWARD, and only on a stage change OBSERVED while
 * it was open. A difference that already existed when it opened means the user chose this
 * screen. Otherwise a changed state token reloads the screen's data, except on Enrich.
 */
export function decideStatus(input: StatusInput): StatusDecision {
  const { view, preview, hasDraft, lastStage, status, loadedToken } = input
  const stage = status.stage
  const movesForward = STAGE_ORDER.indexOf(stage) > STAGE_ORDER.indexOf(view)
  const observed = lastStage !== "" && stage !== lastStage
  const decision = (action: StatusAction): StatusDecision => ({ action, lastStage: stage })

  if (!preview && movesForward && observed) {
    return decision(hasDraft ? NOTHING : { kind: "navigate", stage })
  }
  // Enrich only waits, and draws what it says from the status itself: a store that changed
  // under it has nothing for it to read again.
  if (view !== "enrich" && status.state_token && status.state_token !== loadedToken) {
    return decision(hasDraft ? NOTHING : { kind: "reload" })
  }
  return decision(NOTHING)
}
