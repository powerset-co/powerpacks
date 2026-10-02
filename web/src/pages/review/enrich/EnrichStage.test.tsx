import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { reviewStatus } from "@/testing/review-fixture"
import type { EnrichStep, ReviewStatus } from "@/types/review"

import { STARTING, TITLE } from "./copy"
import { EnrichStage } from "./EnrichStage"

function status(step: EnrichStep, pending: Partial<ReviewStatus["pending"]> = {}, minutes = 0): ReviewStatus {
  const nothing = reviewStatus().pending
  return reviewStatus({ step, pending: { ...nothing, ...pending }, minutes_left: minutes })
}

function ring(container: HTMLElement): (string | null)[] {
  return [...container.querySelectorAll(".enrich-ring circle")].map((arc) => arc.getAttribute("class"))
}

afterEach(cleanup)

describe("EnrichStage", () => {
  it("only waits: its moving shape, an empty ring, and nothing to press", () => {
    const { container } = render(<EnrichStage status={null} />)
    expect(screen.getByRole("heading", { name: TITLE })).toBeTruthy()
    expect(screen.getByText(STARTING)).toBeTruthy()
    expect(container.querySelector(".enrich-time-left")?.textContent).toBe("")
    expect(container.querySelector(".enrich-shape")).toBeTruthy()
    expect(ring(container)).toEqual([null, null, null])
    expect(screen.queryByRole("button")).toBeNull()
  })

  it("says the step the run is on, with what is left of it", () => {
    const { rerender } = render(
      <EnrichStage status={status("research", { lookups: 25, linkedin_checks: 300 })} />,
    )
    expect(screen.getByText("Looking up 25 people")).toBeTruthy()
    rerender(<EnrichStage status={status("identity", { lookups: 2, linkedin_checks: 300 })} />)
    expect(screen.getByText("Checking 300 LinkedIn profiles")).toBeTruthy()
    rerender(<EnrichStage status={status("relationships", { unsure: 1 })} />)
    expect(screen.getByText("Settling the unsure matches for 1 person")).toBeTruthy()
    rerender(<EnrichStage status={status("synthetic", { profiles: 4 })} />)
    expect(screen.getByText("Finishing up")).toBeTruthy()
  })

  it("names the step without a count when the store counts nothing left of it", () => {
    const { rerender } = render(<EnrichStage status={status("research")} />)
    expect(screen.getByText("Looking up people")).toBeTruthy()
    rerender(<EnrichStage status={status("profiles")} />)
    expect(screen.getByText("Checking LinkedIn profiles")).toBeTruthy()
    rerender(<EnrichStage status={status("relationships")} />)
    expect(screen.getByText("Settling the unsure matches")).toBeTruthy()
  })

  it("fills the ring part by part as the run passes them", () => {
    const { container, rerender } = render(<EnrichStage status={status("research")} />)
    expect(ring(container)).toEqual(["now", null, null])
    rerender(<EnrichStage status={status("profiles")} />)
    expect(ring(container)).toEqual(["done", "now", null])
    rerender(<EnrichStage status={status("settle")} />)
    expect(ring(container)).toEqual(["done", "done", "now"])
  })

  it("says about how long the rest takes, as the server estimates it", () => {
    const { rerender } = render(<EnrichStage status={status("research", { lookups: 210 }, 5)} />)
    expect(screen.getByText("about 5 min left")).toBeTruthy()
    rerender(<EnrichStage status={status("identity", { linkedin_checks: 181 }, 0)} />)
    expect(screen.getByText("under a minute left")).toBeTruthy()
  })
})
