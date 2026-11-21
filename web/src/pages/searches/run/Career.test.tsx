import { cleanup, render } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { JORDAN } from "@/testing/searches-fixture"

import { Career } from "./Career"

afterEach(cleanup)

describe("Career", () => {
  it("marks the matched role by its index in the full profile, not its place in a light row", () => {
    const [current, past] = JORDAN.positions
    if (!current || !past) throw new Error("fixture has two positions")
    const row = {
      ...JORDAN,
      positions: [current, { ...past, title: "Founder", position_index: 7 }],
      matched_positions: [7],
    }
    render(<Career row={row} />)
    const roles = [...document.querySelectorAll<HTMLElement>("[data-positions] li")]
    expect(roles.map((role) => role.dataset.matched)).toEqual(["false", "true"])
  })
})
