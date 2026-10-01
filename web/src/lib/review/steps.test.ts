import { describe, expect, it } from "vitest"

import { decisionProgress, pageProgress, reviewSteps } from "@/testing/review-fixture"

import { ACTIVE_STEP, liveSteps, stepCount, stepMarker, stepState } from "./steps"

describe("stepMarker", () => {
  it("draws the check for a complete step with nothing left", () => {
    expect(stepMarker({ number: 1, complete: true, count: 0 })).toBe("✓")
  })

  it("draws the number while people are left or the step is not complete", () => {
    expect(stepMarker({ number: 1, complete: true, count: 2 })).toBe("1")
    expect(stepMarker({ number: 3, complete: false, count: 0 })).toBe("3")
  })
})

describe("stepState", () => {
  it("highlights the active step whether or not it is finished", () => {
    expect(stepState({ number: 1, complete: true, count: 0 }, true)).toBe("active")
    expect(stepState({ number: 1, complete: false, count: 4 }, true)).toBe("active")
  })

  it("marks an inactive step complete only when it is finished", () => {
    expect(stepState({ number: 2, complete: true, count: 0 }, false)).toBe("complete")
    expect(stepState({ number: 2, complete: true, count: 1 }, false)).toBe("idle")
    expect(stepState({ number: 2, complete: false, count: 0 }, false)).toBe("idle")
  })
})

describe("stepCount", () => {
  it("says how many are left, and nothing for none", () => {
    expect(stepCount(4)).toBe("4 left")
    expect(stepCount(0)).toBe("")
  })
})

describe("ACTIVE_STEP", () => {
  it("lights step 3 on the done screen", () => {
    expect(ACTIVE_STEP).toEqual({ worth: 0, enrich: 1, linkedin: 2, done: 2 })
  })
})

describe("liveSteps", () => {
  it("repaints steps 1 and 3 from a click response and leaves step 2 and `complete` alone", () => {
    const steps = reviewSteps(pageProgress({ worth_pending: 3, linkedin_pending: 4 }))
    const live = liveSteps(steps, decisionProgress({ worth_pending: 0, linkedin_pending: 9 }))
    expect(live.map((step) => step.count)).toEqual([0, 0, 9])
    expect(live.map((step) => step.complete)).toEqual([false, false, false])
    expect(stepMarker(live[0] ?? steps[0])).toBe("1")
  })
})
