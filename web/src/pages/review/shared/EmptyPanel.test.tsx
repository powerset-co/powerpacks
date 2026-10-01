import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { EmptyPanel } from "./EmptyPanel"
import { StageCheck } from "./StageCheck"

afterEach(cleanup)

describe("EmptyPanel and StageCheck", () => {
  it("draws a titled panel around its body", () => {
    const { container } = render(
      <EmptyPanel title="Contacts Enriched" className="enrich-state">
        <p>Body</p>
      </EmptyPanel>,
    )
    expect(container.firstElementChild?.className).toBe("empty-state enrich-state")
    expect(container.firstElementChild?.textContent).toBe("Contacts EnrichedBody")
  })

  it("draws the check over the finished stage's words, or alone", () => {
    const { container, rerender } = render(<StageCheck message="People Reviewed" />)
    expect(container.firstElementChild?.className).toBe("empty-state stage-complete")
    expect(container.querySelector(".empty-mark")?.textContent).toBe("✓")
    expect(screen.getByRole("heading").textContent).toBe("People Reviewed")
    rerender(<StageCheck message="" />)
    expect(container.querySelector("h2")?.textContent).toBe("")
  })

  it("says the next stage is being prepared, under a moving bar", () => {
    const { container } = render(<StageCheck message="Contacts Enriched" />)
    expect(container.querySelector(".stage-complete > p")?.textContent).toBe("Preparing Next Stage")
    const bar = screen.getByRole("progressbar", { name: "Preparing Next Stage" })
    expect(bar.className).toBe("enrich-progress indeterminate")
    expect(bar.firstElementChild?.className).toBe("enrich-progress-fill")
  })
})
