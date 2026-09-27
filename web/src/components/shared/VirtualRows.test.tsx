import { cleanup, render } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it } from "vitest"

import { must } from "@/lib/must"

import { VirtualRows } from "./VirtualRows"

const ROW = 36
const VIEWPORT = 360
const PEOPLE = Array.from({ length: 1000 }, (_, index) => ({ id: `p${index}`, name: `Casey Delta ${index}` }))

// jsdom has no layout; the virtualizer reads the scroll element's offset size.
function stubViewportSize(height: number) {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => height })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 800 })
}

// A measured row wrapper (data-index) is one row tall at even indexes, two at odd ones.
function measuredHeight(this: HTMLElement): number {
  const index = this.dataset.index
  if (index === undefined) return VIEWPORT
  return Number(index) % 2 ? 2 * ROW : ROW
}

function stubMeasuredRows() {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: measuredHeight })
}

function rowWrappers(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(".relative > div")]
}

afterEach(cleanup)

describe("VirtualRows", () => {
  beforeEach(() => stubViewportSize(VIEWPORT))

  it("mounts only the visible window plus overscan inside a full-height spacer", () => {
    const { container } = render(
      <VirtualRows
        items={PEOPLE}
        rowHeight={ROW}
        overscan={5}
        getKey={(person) => person.id}
        renderRow={(person) => <div data-row>{person.name}</div>}
      />,
    )
    const rows = container.querySelectorAll("[data-row]")
    expect(rows.length).toBe(VIEWPORT / ROW + 5)
    expect(rows[0]?.textContent).toBe("Casey Delta 0")
    const spacer = must(container.querySelector<HTMLElement>(".relative"))
    expect(spacer.style.height).toBe(`${PEOPLE.length * ROW}px`)
  })

  it("renders nothing while the viewport has no height", () => {
    stubViewportSize(0)
    const { container } = render(
      <VirtualRows
        items={PEOPLE}
        rowHeight={ROW}
        getKey={(person) => person.id}
        renderRow={(person) => <div data-row>{person.name}</div>}
      />,
    )
    expect(container.querySelectorAll("[data-row]").length).toBe(0)
  })

  it("gives every fixed row the row height and no measuring hooks", () => {
    const { container } = render(
      <VirtualRows
        items={PEOPLE}
        rowHeight={ROW}
        overscan={0}
        getKey={(person) => person.id}
        renderRow={(person) => <div data-row>{person.name}</div>}
      />,
    )
    const wrappers = rowWrappers(container)
    expect(wrappers.map((row) => row.style.transform)).toEqual(
      Array.from({ length: VIEWPORT / ROW }, (_, index) => `translateY(${index * ROW}px)`),
    )
    for (const row of wrappers) {
      expect(row.style.height).toBe(`${ROW}px`)
      expect(row.hasAttribute("data-index")).toBe(false)
    }
  })

  it("measures rows and places each below the measured rows above it", () => {
    stubMeasuredRows()
    const { container } = render(
      <VirtualRows
        items={PEOPLE}
        rowHeight={ROW}
        measure
        overscan={0}
        getKey={(person) => person.id}
        renderRow={(person) => <div data-row>{person.name}</div>}
      />,
    )
    const wrappers = rowWrappers(container)
    // 36 + 72 + 36 + 72 + 36 + 72 = 324 < 360, so seven rows reach the viewport's bottom.
    expect(wrappers.map((row) => row.dataset.index)).toEqual(["0", "1", "2", "3", "4", "5", "6"])
    expect(wrappers.map((row) => row.style.transform)).toEqual(
      [0, 36, 108, 144, 216, 252, 324].map((start) => `translateY(${start}px)`),
    )
    for (const row of wrappers) expect(row.style.height).toBe("")
    const spacer = must(container.querySelector<HTMLElement>(".relative"))
    // The first render mounts a window.innerHeight's worth at the estimate; every one of those
    // was measured, and each tall one adds a row to the estimated total.
    const tall = Math.floor(Math.ceil(window.innerHeight / ROW) / 2)
    expect(spacer.style.height).toBe(`${(PEOPLE.length + tall) * ROW}px`)
  })
})
