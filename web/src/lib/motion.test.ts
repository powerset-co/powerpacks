import { describe, expect, it } from "vitest"

import { cssMs } from "./motion"

describe("cssMs", () => {
  it("reads milliseconds and seconds alike", () => {
    expect(cssMs("200ms")).toBe(200)
    expect(cssMs(".2s")).toBe(200)
    expect(cssMs(" 120ms ")).toBe(120)
    expect(cssMs("1.5s")).toBe(1500)
  })
})
