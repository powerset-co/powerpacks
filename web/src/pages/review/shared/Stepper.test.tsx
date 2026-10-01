import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, describe, expect, it } from "vitest"

import { enrichmentPanel, pageProgress, reviewSteps } from "@/testing/review-fixture"
import type { ReviewStep, ReviewView } from "@/types/review"

import { Stepper } from "./Stepper"

afterEach(cleanup)

function renderStepper(steps: readonly ReviewStep[], view: ReviewView) {
  render(
    <MemoryRouter>
      <Stepper steps={steps} view={view} />
    </MemoryRouter>,
  )
  return screen.getAllByRole("link")
}

describe("Stepper", () => {
  it("draws three linked steps with a line between, each opening its stage as a preview", () => {
    const steps = renderStepper(reviewSteps(), "worth")
    expect(steps.map((step) => step.getAttribute("href"))).toEqual([
      "/review?stage=worth&preview=1",
      "/review?stage=enrich&preview=1",
      "/review?stage=linkedin&preview=1",
    ])
    const nav = screen.getByRole("navigation", { name: "Progress" })
    expect(nav.querySelectorAll(".step-line")).toHaveLength(2)
    expect([...nav.children].map((child) => child.tagName)).toEqual(["A", "I", "A", "I", "A"])
  })

  it("shows the number and how many are left, and highlights the active step", () => {
    const [worth, enrich, linkedin] = renderStepper(
      reviewSteps(pageProgress({ worth_pending: 3, linkedin_pending: 4 })),
      "worth",
    )
    expect(worth?.textContent).toBe("1Review Decisions3 left")
    expect(worth?.className).toBe("step active")
    expect(enrich?.textContent).toBe("2Enrich Contacts")
    expect(enrich?.className).toBe("step")
    expect(linkedin?.textContent).toBe("3Check LinkedIn4 left")
  })

  it("checks a complete step with nothing left", () => {
    const progress = pageProgress({ worth_pending: 0, linkedin_pending: 2 })
    const [worth, enrich, linkedin] = renderStepper(
      reviewSteps(progress, enrichmentPanel({ mode: "completed" })),
      "linkedin",
    )
    expect(worth?.textContent).toBe("✓Review Decisions")
    expect(worth?.className).toBe("step complete")
    expect(enrich?.className).toBe("step complete")
    expect(linkedin?.className).toBe("step active")
    expect(linkedin?.querySelector("small")?.textContent).toBe("2 left")
  })

  it("keeps the check on the active step, and lights step 3 on the done screen", () => {
    const progress = pageProgress({ worth_pending: 0, linkedin_pending: 0 })
    const [, , linkedin] = renderStepper(
      reviewSteps(progress, enrichmentPanel({ mode: "completed" })),
      "done",
    )
    expect(linkedin?.className).toBe("step active")
    expect(linkedin?.textContent).toBe("✓Check LinkedIn")
  })

  it("shows no step complete while synthesis is pending", () => {
    const progress = pageProgress({ worth_pending: 0, linkedin_pending: 0, synthesize_pending: 5 })
    const steps = renderStepper(reviewSteps(progress, enrichmentPanel({ mode: "completed" })), "enrich")
    expect(steps.map((step) => step.querySelector("span")?.textContent)).toEqual(["1", "2", "3"])
    expect(steps.map((step) => step.className)).toEqual(["step", "step active", "step"])
  })
})
