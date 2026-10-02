// How long the rest of an enrichment run takes, from what the store says is left.
//
// The steps are not alike, so each is timed on its own. Measured on two real runs (25 and 210
// lookups): the lookup provider answers nothing for about 40 seconds, most of a batch within two
// minutes, and its last few after about four, whatever the batch size; profile fetches and
// LinkedIn checks together clear about ten a second; an unsure match takes about half a second.

import type { EnrichPending } from "@/types/review"

import { aboutMinutesLeft, UNDER_A_MINUTE } from "./copy"

/** A batch of lookups, start to last answer. */
const LOOKUP_BATCH_MS = 240_000
/** Near the end of the usual batch time, the last answers are still this far off. */
const LOOKUP_LAST_MS = 30_000
/** Past the usual batch time: the last few lookups of a batch are the slow ones. */
const LOOKUP_TAIL_MS = 90_000
/** Past the usual batch time with too few back to read a pace, never promise less than this. */
const LOOKUP_FLOOR_MS = 60_000
/** The share of a batch that must answer before its pace means anything: the first few trickle in. */
const PACE_SHARE = 0.1
/** A found LinkedIn is fetched, then checked. */
const CHECK_MS = 100
/** The share of lookups that find a LinkedIn to check. */
const FOUND_SHARE = 0.85
const UNSURE_MS = 500
/** Settling worth and writing the profiles: seconds, whatever the count. */
const TIDY_MS = 10_000
const MINUTE_MS = 60_000

/** What was left, and when it was read. */
export interface Reading {
  at: number
  pending: EnrichPending
}

/**
 * The lookups still out. Within the provider's usual batch time it is what remains of that time:
 * on both measured runs the last answer came at about four minutes, however fast the first ones
 * did. A batch that runs past it is a bigger one, read by its pace once a fair share has answered.
 */
function lookupMs(start: Reading, now: Reading): number {
  const left = now.pending.lookups
  if (!left) return 0

  const elapsed = now.at - start.at
  if (elapsed < LOOKUP_BATCH_MS) return Math.max(LOOKUP_BATCH_MS - elapsed, LOOKUP_LAST_MS)

  const back = start.pending.lookups - left
  if (back < start.pending.lookups * PACE_SHARE) return LOOKUP_FLOOR_MS
  return Math.max((left * elapsed) / back, LOOKUP_TAIL_MS)
}

/** Everything after the lookups, counting the checks the lookups still out will add. */
function afterLookupsMs(pending: EnrichPending): number {
  const checks = pending.linkedin_checks + pending.lookups * FOUND_SHARE
  return checks * CHECK_MS + pending.unsure * UNSURE_MS + TIDY_MS
}

/** The rest of the run, from the first reading on this screen and the latest. */
export function timeLeft(start: Reading, now: Reading): string {
  const remaining = lookupMs(start, now) + afterLookupsMs(now.pending)
  return remaining < MINUTE_MS ? UNDER_A_MINUTE : aboutMinutesLeft(Math.round(remaining / MINUTE_MS))
}
