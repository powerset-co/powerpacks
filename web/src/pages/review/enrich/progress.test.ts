import { describe, expect, it } from "vitest"

import type { EnrichPending } from "@/types/review"

import { doingNow, FINISHING } from "./copy"
import { type Reading, timeLeft } from "./progress"

const NOTHING: EnrichPending = { lookups: 0, linkedin_checks: 0, unsure: 0, profiles: 0 }

function reading(seconds: number, pending: Partial<EnrichPending>): Reading {
  return { at: seconds * 1000, pending: { ...NOTHING, ...pending } }
}

describe("doingNow", () => {
  it("names the first step with anything left, in the order the run takes them", () => {
    expect(doingNow({ lookups: 25, linkedin_checks: 653, unsure: 310, profiles: 4 })).toBe(
      "Looking up 25 people",
    )
    expect(doingNow({ ...NOTHING, linkedin_checks: 1653, unsure: 310 })).toBe(
      "Checking 1,653 LinkedIn profiles",
    )
    expect(doingNow({ ...NOTHING, unsure: 1, profiles: 4 })).toBe("Settling the unsure matches for 1 person")
    expect(doingNow({ ...NOTHING, profiles: 4 })).toBe("Writing profiles for 4 people with no LinkedIn")
  })

  it("says it is finishing up when nothing is counted as left", () => {
    expect(doingNow(NOTHING)).toBe(FINISHING)
  })
})

// The readings below are two real runs of 210 lookups, as the status reported them.
describe("timeLeft", () => {
  const start = reading(0, { lookups: 210, profiles: 43 })

  it("starts at the provider's usual batch time, before any answer is back", () => {
    expect(timeLeft(start, start)).toBe("about 4 min left")
    expect(timeLeft(start, reading(30, { lookups: 210, profiles: 43 }))).toBe("about 4 min left")
  })

  it("does not read a pace from the first few answers", () => {
    expect(timeLeft(start, reading(36, { lookups: 209, linkedin_checks: 1, profiles: 43 }))).toBe(
      "about 4 min left",
    )
    expect(timeLeft(start, reading(46, { lookups: 207, linkedin_checks: 3, profiles: 43 }))).toBe(
      "about 4 min left",
    )
  })

  it("counts down the usual batch time while the lookups come back", () => {
    expect(timeLeft(start, reading(66, { lookups: 168, linkedin_checks: 35, profiles: 50 }))).toBe(
      "about 3 min left",
    )
    expect(timeLeft(start, reading(96, { lookups: 102, linkedin_checks: 90, profiles: 58 }))).toBe(
      "about 3 min left",
    )
    expect(timeLeft(start, reading(152, { lookups: 1, linkedin_checks: 181, profiles: 62 }))).toBe(
      "about 2 min left",
    )
    expect(timeLeft(start, reading(230, { lookups: 1, linkedin_checks: 181, profiles: 62 }))).toBe(
      "under a minute left",
    )
  })

  it("is the checks alone once every lookup is back", () => {
    expect(timeLeft(start, reading(243, { linkedin_checks: 181, profiles: 63 }))).toBe("under a minute left")
    expect(timeLeft(start, reading(263, { unsure: 13, profiles: 76 }))).toBe("under a minute left")
  })

  it("counts thousands of checks in minutes", () => {
    const checks = reading(0, { linkedin_checks: 1800 })
    expect(timeLeft(checks, checks)).toBe("about 3 min left")
  })

  it("follows the pace alone once a batch runs past the usual time", () => {
    const big = reading(0, { lookups: 4000 })
    expect(timeLeft(big, reading(600, { lookups: 2000 }))).toBe("about 13 min left")
  })

  it("never promises less than a minute past the usual time while too few lookups have answered", () => {
    expect(timeLeft(start, reading(400, { lookups: 210 }))).toBe("about 1 min left")
  })
})
