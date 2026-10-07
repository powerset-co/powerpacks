// The review page's waits, in milliseconds.

/** A message stays this long. */
export const TOAST_MS = 1800
/** An error stays long enough to read. */
export const TOAST_ERROR_MS = 6000
/** A card's contents (or a row, or the stage) leave for this long before the swap: the
 *  --leave-ms of styles/base.css, which then brings the new contents in over
 *  --arrive-ms. */
const FADE_MS = 150

/** The fade's wait: it exists only for the animation, so it is 0 under reduced motion. */
export function fadeMs(reducedMotion: boolean): number {
  return reducedMotion ? 0 : FADE_MS
}

export function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}
