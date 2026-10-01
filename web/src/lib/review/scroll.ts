// The scroll cue's two rules (reconcile_review.js `refreshScrollCues`, `wireScrollShell`).

/** Slack around both ends, so a sub-pixel remainder never shows the cue. */
const SLACK_PX = 4
const MIN_STEP_PX = 160
const STEP_OF_VIEW = 0.7

interface Scroller {
  scrollHeight: number
  clientHeight: number
  scrollTop: number
}

/** The box overflows and is not at its end. */
export function moreBelow({ scrollHeight, clientHeight, scrollTop }: Scroller): boolean {
  return scrollHeight > clientHeight + SLACK_PX && scrollTop + clientHeight < scrollHeight - SLACK_PX
}

/** One press of the cue: 70% of the box, at least 160px. */
export function scrollStep(clientHeight: number): number {
  return Math.max(MIN_STEP_PX, clientHeight * STEP_OF_VIEW)
}
