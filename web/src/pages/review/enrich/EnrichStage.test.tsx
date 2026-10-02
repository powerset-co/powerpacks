import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { MOVES_ON, STARTING, TITLE } from "./copy"
import { EnrichStage, type Waiting } from "./EnrichStage"

const NOTHING = { lookups: 0, linkedin_checks: 0, unsure: 0, profiles: 0 }

function waiting(at: number, pending: Partial<Waiting["pending"]>): Waiting {
  return { at, pending: { ...NOTHING, ...pending } }
}

afterEach(cleanup)

describe("EnrichStage", () => {
  it("only waits: its moving shape, nothing to press, and that it moves on by itself", () => {
    const { container } = render(<EnrichStage waiting={null} />)
    expect(screen.getByRole("heading", { name: TITLE })).toBeTruthy()
    expect(screen.getByText(STARTING)).toBeTruthy()
    expect(screen.getByText(MOVES_ON)).toBeTruthy()
    expect(container.querySelector(".enrich-shape")).toBeTruthy()
    expect(screen.queryByRole("button")).toBeNull()
  })

  it("says what is being done from the latest status read", () => {
    const { rerender } = render(<EnrichStage waiting={waiting(0, { lookups: 25, linkedin_checks: 300 })} />)
    expect(screen.getByText("Looking up 25 people")).toBeTruthy()
    rerender(<EnrichStage waiting={waiting(10_000, { linkedin_checks: 300 })} />)
    expect(screen.getByText("Checking 300 LinkedIn profiles")).toBeTruthy()
  })

  it("says how long the rest takes once the count has gone down", () => {
    const { rerender } = render(<EnrichStage waiting={waiting(0, { linkedin_checks: 320 })} />)
    expect(screen.getByText(MOVES_ON)).toBeTruthy()
    rerender(<EnrichStage waiting={waiting(60_000, { linkedin_checks: 220 })} />)
    expect(screen.getByText("about 2 min left")).toBeTruthy()
    expect(screen.queryByText(MOVES_ON)).toBeNull()
  })
})
