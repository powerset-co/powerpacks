import { describe, expect, it } from "vitest"

import { nextOpenIndex } from "./advance"

const ROWS = ["a", "b", "c", "d"]
const key = (row: string) => row
const without = (id: string) => ROWS.filter((row) => row !== id)

describe("nextOpenIndex", () => {
  it("opens the row that took the done row's place", () => {
    expect(nextOpenIndex(without("b"), key, "b", 1)).toBe(1)
  })

  it("walks backwards from the end once the list is shorter", () => {
    expect(nextOpenIndex(without("d"), key, "d", 3)).toBe(2)
  })

  it("closes on the last one", () => {
    expect(nextOpenIndex([], key, "a", 0)).toBeNull()
  })

  it("moves down when the done row stays, and stops at the end", () => {
    expect(nextOpenIndex(ROWS, key, "a", 0)).toBe(1)
    expect(nextOpenIndex(ROWS, key, "d", 3)).toBeNull()
  })
})
