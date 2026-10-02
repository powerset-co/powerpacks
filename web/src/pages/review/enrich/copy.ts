// The Enrich stage's words: the agent is doing the work, and this page says what is left.

import type { EnrichPending } from "@/types/review"

export const TITLE = "Working on your network"

/** Before the first status read, and when nothing is counted as left. */
export const STARTING = "Getting started"
export const FINISHING = "Finishing up"

function people(count: number): string {
  return count === 1 ? "1 person" : `${count.toLocaleString("en-US")} people`
}

/** What is being done now: the first step, in the order the run takes them, with anything left. */
export function doingNow(pending: EnrichPending): string {
  if (pending.lookups) return `Looking up ${people(pending.lookups)}`
  if (pending.linkedin_checks) {
    const profiles = pending.linkedin_checks === 1 ? "profile" : "profiles"
    return `Checking ${pending.linkedin_checks.toLocaleString("en-US")} LinkedIn ${profiles}`
  }
  if (pending.unsure) return `Settling the unsure matches for ${people(pending.unsure)}`
  if (pending.profiles) return `Writing profiles for ${people(pending.profiles)} with no LinkedIn`
  return FINISHING
}

export const UNDER_A_MINUTE = "under a minute left"

/** The rest of the work, in whole minutes. */
export function aboutMinutesLeft(minutes: number): string {
  return `about ${minutes} min left`
}
