import { describe, expect, it } from "vitest"

import { doingNow, FINISHING } from "./copy"
import { timeLeft, totalLeft } from "./progress"

const NOTHING = { lookups: 0, linkedin_checks: 0, unsure: 0, profiles: 0 }

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

describe("totalLeft", () => {
  it("adds the four counts", () => {
    expect(totalLeft({ lookups: 1, linkedin_checks: 2, unsure: 3, profiles: 4 })).toBe(10)
  })
})

describe("timeLeft", () => {
  const start = { at: 0, left: 320 }

  it("says nothing until the count has gone down over a few readings", () => {
    expect(timeLeft(start, { at: 60_000, left: 320 })).toBe("")
    expect(timeLeft(start, { at: 10_000, left: 300 })).toBe("")
  })

  it("is what remains at the pace seen so far", () => {
    // 100 a minute, 220 to go.
    expect(timeLeft(start, { at: 60_000, left: 220 })).toBe("about 2 min left")
  })

  it("says under a minute near the end", () => {
    expect(timeLeft(start, { at: 60_000, left: 20 })).toBe("under a minute left")
  })

  it("says nothing once nothing is left", () => {
    expect(timeLeft(start, { at: 60_000, left: 0 })).toBe("")
  })
})
