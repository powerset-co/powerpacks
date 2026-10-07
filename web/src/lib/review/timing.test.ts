import { describe, expect, it } from "vitest"

import { fadeMs } from "./timing"

describe("fadeMs", () => {
  it("is the 140 ms card fade, and nothing under reduced motion", () => {
    expect(fadeMs(false)).toBe(140)
    expect(fadeMs(true)).toBe(0)
  })
})
