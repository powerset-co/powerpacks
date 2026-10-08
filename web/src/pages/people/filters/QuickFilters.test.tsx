import { cleanup, render } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { QUICK } from "@/lib/people/facets"
import { QuickFilters } from "./QuickFilters"

afterEach(cleanup)
it("hides empty quick filters and orders available filters by count", () => {
  const { container } = render(
    <QuickFilters
      filters={new Map()}
      counts={QUICK.map((_, i) => (i === 0 ? 2 : i === 1 ? 12 : 0))}
      onPick={vi.fn()}
    />,
  )
  expect(
    [...container.querySelectorAll("button")].map((button) => button.getAttribute("data-quick-index")),
  ).toEqual(["1", "0"])
})

it("pins Has logbook last even when it has the largest count", () => {
  const index = QUICK.findIndex((quick) => quick.set.logbook)
  expect(index).toBeGreaterThanOrEqual(0)
  const { container } = render(
    <QuickFilters
      filters={new Map()}
      counts={QUICK.map((_, i) => (i === index ? 500 : i === 0 ? 2 : 0))}
      onPick={vi.fn()}
    />,
  )
  const buttons = [...container.querySelectorAll("button")]
  expect(buttons.at(-1)?.textContent).toContain("Has logbook")
  expect(buttons[0]?.textContent).toContain("Family")
})
