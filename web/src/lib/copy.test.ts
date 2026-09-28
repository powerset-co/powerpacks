import { describe, expect, it } from "vitest"

import { countOf, monthYear, plural } from "./copy"

describe("copy", () => {
  it("pluralizes person as people", () => {
    expect(plural(1, "person")).toBe("1 person")
    expect(plural(2, "person")).toBe("2 people")
    expect(plural(4, "search")).toBe("4 searches")
  })

  it("counts all, or some of all", () => {
    expect(countOf(125, 125, "result")).toBe("125 results")
    expect(countOf(50, 125, "result")).toBe("50 of 125 results")
    expect(countOf(1, 1, "result")).toBe("1 result")
    expect(countOf(3, 10, "person")).toBe("3 of 10 people")
  })

  it("writes a month and year, and keeps what is not a date", () => {
    expect(monthYear("2026-09-15")).toBe("Sep 2026")
    expect(monthYear("")).toBe("")
    expect(monthYear("soon")).toBe("soon")
  })
})
