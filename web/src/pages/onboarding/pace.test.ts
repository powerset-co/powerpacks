import { describe, expect, it } from "vitest"

import { leftText, secondsLeft } from "./pace"

describe("secondsLeft", () => {
  it("needs two samples that progressed", () => {
    expect(secondsLeft([], 100)).toBeNull()
    expect(secondsLeft([{ at: 0, done: 10 }], 100)).toBeNull()
    expect(
      secondsLeft(
        [
          { at: 0, done: 10 },
          { at: 5_000, done: 10 },
        ],
        100,
      ),
    ).toBeNull()
  })

  it("divides what is left by the pace between the first and the latest sample", () => {
    const samples = [
      { at: 0, done: 100 },
      { at: 30_000, done: 400 },
      { at: 60_000, done: 700 },
    ]
    // 600 in 60 s = 10 a second; 1,480 - 700 = 780 left -> 78 s.
    expect(secondsLeft(samples, 1_480)).toBe(78)
  })

  it("has nothing left at the total", () => {
    expect(
      secondsLeft(
        [
          { at: 0, done: 0 },
          { at: 1_000, done: 50 },
        ],
        50,
      ),
    ).toBeNull()
  })
})

describe("leftText", () => {
  it("rounds to minutes, then hours", () => {
    expect(leftText(20)).toBe("under a minute left")
    expect(leftText(78)).toBe("about 1 min left")
    expect(leftText(4 * 60 + 20)).toBe("about 4 min left")
    expect(leftText(3 * 3600 + 100)).toBe("about 3 h left")
  })
})
