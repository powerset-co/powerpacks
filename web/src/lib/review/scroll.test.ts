import { describe, expect, it } from "vitest"

import { moreBelow, scrollStep } from "./scroll"

describe("the scroll cue", () => {
  it("shows while the box overflows and is not at its end", () => {
    expect(moreBelow({ scrollHeight: 900, clientHeight: 400, scrollTop: 0 })).toBe(true)
    expect(moreBelow({ scrollHeight: 900, clientHeight: 400, scrollTop: 497 })).toBe(false)
    expect(moreBelow({ scrollHeight: 404, clientHeight: 400, scrollTop: 0 })).toBe(false)
  })

  it("scrolls 70% of the box, at least 160px", () => {
    expect(scrollStep(500)).toBe(350)
    expect(scrollStep(100)).toBe(160)
  })
})
