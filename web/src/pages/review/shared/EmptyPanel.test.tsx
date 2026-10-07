import { cleanup, render } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { EmptyPanel } from "./EmptyPanel"

afterEach(cleanup)

describe("EmptyPanel", () => {
  it("draws a titled panel around its body", () => {
    const { container } = render(
      <EmptyPanel title="Contacts Enriched" className="enrich-state">
        <p>Body</p>
      </EmptyPanel>,
    )
    expect(container.firstElementChild?.className).toBe("empty-state enrich-state")
    expect(container.firstElementChild?.textContent).toBe("Contacts EnrichedBody")
  })
})
