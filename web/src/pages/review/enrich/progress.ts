// How much the waiting screen says is left, and how long that takes at the pace seen so far.

import type { EnrichPending } from "@/types/review"

import { aboutMinutesLeft, UNDER_A_MINUTE } from "./copy"

/** Everything enrichment still has to do, as one count. */
export function totalLeft(pending: EnrichPending): number {
  return pending.lookups + pending.linkedin_checks + pending.unsure + pending.profiles
}

/** What was left, and when it was read. */
export interface Reading {
  at: number
  left: number
}

/** Too little has been watched to say how long the rest will take. */
const PACE_SETTLE_MS = 15_000
const MINUTE_MS = 60_000

/**
 * The time the rest takes at the pace since `start`; "" until the count has gone down over a
 * few readings, and once nothing is left.
 */
export function timeLeft(start: Reading, now: Reading): string {
  const done = start.left - now.left
  const elapsed = now.at - start.at
  if (done <= 0 || elapsed < PACE_SETTLE_MS || now.left <= 0) return ""

  const remaining = (now.left * elapsed) / done
  return remaining < MINUTE_MS ? UNDER_A_MINUTE : aboutMinutesLeft(Math.round(remaining / MINUTE_MS))
}
