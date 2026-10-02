// The review page's waits, in milliseconds.

/** A message stays this long. */
export const TOAST_MS = 1800
/** An error stays long enough to read. */
export const TOAST_ERROR_MS = 6000
/** The Enrich screen reads the status this often while the agent works. */
export const STATUS_POLL_MS = 10_000
/** The stage check holds this long before the next screen loads; not an animation wait. */
export const STAGE_CHECK_MS = 650
/** The Enrich screen says it is done for this long, its ring full, before the review opens. */
export const ENRICHED_MS = 2400
/** A card's contents (or a row, or the stage) fade out for this long before the swap
 *  (styles/base.css and worth.css fade for the same 100 ms, and a new card's contents fade in
 *  for 180 ms). */
const FADE_MS = 100

/** The fade's wait: it exists only for the animation, so it is 0 under reduced motion. */
export function fadeMs(reducedMotion: boolean): number {
  return reducedMotion ? 0 : FADE_MS
}

export function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}
