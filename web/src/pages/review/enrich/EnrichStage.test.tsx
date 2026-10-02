import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { reviewStatus } from "@/testing/review-fixture"
import type { EnrichStep, ReviewStatus } from "@/types/review"

import { ENRICHED, OPENING_REVIEW, STARTING, TITLE } from "./copy"
import { EnrichStage } from "./EnrichStage"

function status(step: EnrichStep, pending: Partial<ReviewStatus["pending"]> = {}, minutes = 0): ReviewStatus {
  const nothing = reviewStatus().pending
  return reviewStatus({ step, pending: { ...nothing, ...pending }, minutes_left: minutes })
}

/** How many parts of the ring are filled, and which part breathes; null where there is none. */
function ring(container: HTMLElement): { done: string | null; now: string | null } {
  const arc = (name: string, attribute: string) =>
    container.querySelector(`.enrich-ring .${name}`)?.getAttribute(attribute) ?? null
  return { done: arc("done", "stroke-dasharray"), now: arc("now", "stroke-dashoffset") }
}

afterEach(cleanup)

describe("EnrichStage", () => {
  it("only waits: its moving shape, an empty ring, and nothing to press", () => {
    const { container } = render(<EnrichStage done={false} status={null} />)
    expect(screen.getByRole("heading", { name: TITLE })).toBeTruthy()
    expect(screen.getByText(STARTING)).toBeTruthy()
    expect(container.querySelector(".enrich-time-left")?.textContent).toBe("")
    expect(container.querySelector(".enrich-shape")).toBeTruthy()
    expect(container.querySelector(".enrich-orbit")).toBeTruthy()
    expect(ring(container)).toEqual({ done: null, now: null })
    expect(screen.queryByRole("button")).toBeNull()
  })

  it("says the step the run is on, with what is left of it", () => {
    const { rerender } = render(
      <EnrichStage done={false} status={status("research", { lookups: 25, linkedin_checks: 300 })} />,
    )
    expect(screen.getByText("Looking up 25 people")).toBeTruthy()
    rerender(<EnrichStage done={false} status={status("identity", { lookups: 2, linkedin_checks: 300 })} />)
    expect(screen.getByText("Checking 300 LinkedIn profiles")).toBeTruthy()
    rerender(<EnrichStage done={false} status={status("relationships", { unsure: 1 })} />)
    expect(screen.getByText("Settling the unsure matches for 1 person")).toBeTruthy()
    rerender(<EnrichStage done={false} status={status("synthetic", { profiles: 4 })} />)
    expect(screen.getByText("Finishing up")).toBeTruthy()
  })

  it("names the step without a count when the store counts nothing left of it", () => {
    const { rerender } = render(<EnrichStage done={false} status={status("research")} />)
    expect(screen.getByText("Looking up people")).toBeTruthy()
    rerender(<EnrichStage done={false} status={status("profiles")} />)
    expect(screen.getByText("Checking LinkedIn profiles")).toBeTruthy()
    rerender(<EnrichStage done={false} status={status("relationships")} />)
    expect(screen.getByText("Settling the unsure matches")).toBeTruthy()
  })

  it("fills the ring part by part as the run passes them", () => {
    const { container, rerender } = render(<EnrichStage done={false} status={status("research")} />)
    expect(ring(container)).toEqual({ done: null, now: "0" })
    rerender(<EnrichStage done={false} status={status("profiles")} />)
    expect(ring(container)).toEqual({ done: "1 3", now: "-1" })
    rerender(<EnrichStage done={false} status={status("settle")} />)
    expect(ring(container)).toEqual({ done: "2 3", now: "-2" })
  })

  it("says the run is done, its ring full and still, while the review opens", () => {
    const { container } = render(<EnrichStage done status={status("")} />)
    expect(screen.getByText(ENRICHED)).toBeTruthy()
    expect(screen.getByText(OPENING_REVIEW)).toBeTruthy()
    expect(ring(container)).toEqual({ done: "3 3", now: null })
    expect(container.querySelector(".enrich-orbit")).toBeNull()
    expect(container.querySelector(".enrich-dots")).toBeNull()
  })

  it("says about how long the rest takes, as the server estimates it", () => {
    const { rerender } = render(<EnrichStage done={false} status={status("research", { lookups: 210 }, 5)} />)
    expect(screen.getByText("about 5 min left")).toBeTruthy()
    rerender(<EnrichStage done={false} status={status("identity", { linkedin_checks: 181 }, 0)} />)
    expect(screen.getByText("under a minute left")).toBeTruthy()
  })
})
