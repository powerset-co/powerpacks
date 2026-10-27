// The time a count has left, from the pace so far: the first and the latest sample of
// (when, how many done) give a rate; what is left divided by it is the estimate.

export interface Sample {
  at: number
  done: number
}

/** Seconds left at the pace between the first and the latest sample; null until two samples
 *  show progress, or once nothing is left. */
export function secondsLeft(samples: readonly Sample[], total: number): number | null {
  const first = samples[0]
  const last = samples[samples.length - 1]
  if (samples.length < 2 || !first || !last) return null
  const elapsed = (last.at - first.at) / 1000
  const progressed = last.done - first.done
  if (elapsed <= 0 || progressed <= 0) return null
  const left = total - last.done
  if (left <= 0) return null
  return left / (progressed / elapsed)
}

/** "about 4 min left", "under a minute left", "about 1 h 20 min left". */
export function leftText(seconds: number): string {
  if (seconds < 60) return "under a minute left"
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `about ${minutes} min left`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `about ${hours} h ${rest} min left` : `about ${hours} h left`
}
