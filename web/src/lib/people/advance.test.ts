import { describe, expect, it } from "vitest"

import { decodePeople } from "@/lib/api/people"
import { PAYLOAD } from "@/testing/people-fixture"

import { nextOpenIndex } from "./advance"

const PEOPLE = decodePeople(PAYLOAD)
const ids = PEOPLE.map((row) => row.parent_id)
const without = (id: string) => PEOPLE.filter((row) => row.parent_id !== id)

describe("nextOpenIndex", () => {
  it("opens the person who took the labeled person's place", () => {
    expect(nextOpenIndex(without(ids[1] ?? ""), ids[1] ?? "", 1)).toBe(1)
  })

  it("walks backwards from the end once the list is shorter", () => {
    const last = ids[ids.length - 1] ?? ""
    expect(nextOpenIndex(without(last), last, ids.length - 1)).toBe(ids.length - 2)
  })

  it("closes on the last one", () => {
    expect(nextOpenIndex([], ids[0] ?? "", 0)).toBeNull()
  })

  it("moves down when the labeled person stays, and stops at the end", () => {
    expect(nextOpenIndex(PEOPLE, ids[0] ?? "", 0)).toBe(1)
    expect(nextOpenIndex(PEOPLE, ids[ids.length - 1] ?? "", ids.length - 1)).toBeNull()
  })
})
