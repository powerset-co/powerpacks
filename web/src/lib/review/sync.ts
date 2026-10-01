// What a screen that watches the server does with what it hears: each status read
// (/api/status) and each /api/events message. Only Enrich and Done watch.

import type { ReviewEvent, ReviewStatus, ReviewView } from "@/types/review"

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
 * screen. Otherwise a changed state token reloads the screen's data.
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
  if (status.state_token && status.state_token !== loadedToken) {
    return decision(hasDraft ? NOTHING : { kind: "reload" })
  }
  return decision(NOTHING)
}

/** The running enrichment's receipt, as /api/events carries it. */
export type EnrichmentJob = NonNullable<ReviewEvent["job"]>

/**
 * A mid-run job event with counts updates the Enrich panel in place; every other message
 * (a finished job, a plain change, an unreadable one) re-reads the status. `panelShown` is
 * whether the Enrich panel is on screen to take the numbers.
 */
export function runningJob(event: ReviewEvent | null, panelShown: boolean): EnrichmentJob | null {
  const job = event?.job
  if (!panelShown || !job?.counts || job.status !== "running") return null
  return job
}
