// The Enrich stage's words: the agent is doing the work, and this page says where it is.

import type { EnrichPending, EnrichStep } from "@/types/review"

export const TITLE = "Working on your network"

/** Before the run's first step, and through its last ones. */
export const STARTING = "Getting started"
export const FINISHING = "Finishing up"

/** The run's steps as the three parts the ring shows, in order. */
export const PARTS = ["Look up", "Check LinkedIn", "Settle"] as const

const PART_OF_STEP: Record<Exclude<EnrichStep, "">, number> = {
  research: 0,
  profiles: 1,
  identity: 1,
  relationships: 2,
  settle: 2,
  synthetic: 2,
}

/** The part of the ring the run is on; -1 before it starts. */
export function partOf(step: EnrichStep): number {
  return step === "" ? -1 : PART_OF_STEP[step]
}

/** "people", or how many of them when the store counts any: "1 person", "1,653 people". */
function counted(count: number, one: string, many: string): string {
  if (!count) return many
  return count === 1 ? `1 ${one}` : `${count.toLocaleString("en-US")} ${many}`
}

/** What the run is doing on this step, with how many are left where the store counts them. */
export function doingNow(step: EnrichStep, pending: EnrichPending): string {
  switch (step) {
    case "":
      return STARTING
    case "research":
      return `Looking up ${counted(pending.lookups, "person", "people")}`
    case "profiles":
    case "identity":
      return `Checking ${counted(pending.linkedin_checks, "LinkedIn profile", "LinkedIn profiles")}`
    case "relationships":
      if (!pending.unsure) return "Settling the unsure matches"
      return `Settling the unsure matches for ${counted(pending.unsure, "person", "people")}`
    case "settle":
    case "synthetic":
      return FINISHING
  }
}

/** The rest of the work, in whole minutes as the server estimates them. */
export function timeLeft(minutes: number): string {
  return minutes ? `about ${minutes} min left` : "under a minute left"
}
