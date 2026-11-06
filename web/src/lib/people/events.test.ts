import { describe, expect, it } from "vitest"

import { newestFirst } from "./events"

describe("newestFirst", () => {
  it("puts the newest event on top and undated events last", () => {
    const events = [
      { date: "2019-03", summary: "met" },
      { date: "", summary: "undated" },
      { date: "2024-11-02", summary: "latest" },
      { date: "2021", summary: "middle" },
    ]
    expect(newestFirst(events).map((event) => event.summary)).toEqual(["latest", "middle", "met", "undated"])
  })
})
