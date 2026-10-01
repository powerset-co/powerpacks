// The stepper's rules (rendering.py `_step`, reconcile_review.js `applyProgress`).

import type { DecisionProgress, ReviewStep, ReviewView } from "@/types/review"

export const COMPLETE_MARK = "✓"

/** The step each screen lights; Done has no step of its own and lights the last. */
export const ACTIVE_STEP: Readonly<Record<ReviewView, number>> = { worth: 0, enrich: 1, linkedin: 2, done: 2 }

export type StepState = "active" | "complete" | "idle"

type StepCounts = Pick<ReviewStep, "number" | "complete" | "count">

/** A finished step: complete with nothing left. */
function finished(step: StepCounts): boolean {
  return step.complete && step.count === 0
}

/** The check once the step is finished, else its number. */
export function stepMarker(step: StepCounts): string {
  return finished(step) ? COMPLETE_MARK : String(step.number)
}

/** The active step is highlighted whether or not it is finished. */
export function stepState(step: StepCounts, active: boolean): StepState {
  if (active) return "active"
  return finished(step) ? "complete" : "idle"
}

/** "3 left", or nothing when none are. */
export function stepCount(count: number): string {
  return count ? `${count} left` : ""
}

/** The steps with the counts a decision click repaints: step 1 is the worth queue, step 3 the
 *  LinkedIn queue. Only the counts move; `complete` stays as the page loaded it. */
export function liveSteps<Step extends ReviewStep>(
  steps: readonly Step[],
  progress: DecisionProgress,
): Step[] {
  const counts: Partial<Record<ReviewStep["stage"], number>> = {
    worth: progress.worth_pending,
    linkedin: progress.linkedin_pending,
  }
  return steps.map((step) => ({ ...step, count: counts[step.stage] ?? step.count }))
}
