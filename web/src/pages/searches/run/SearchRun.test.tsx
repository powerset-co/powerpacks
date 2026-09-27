import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { RANKED_NAMES, RUN } from "@/testing/searches-fixture"

import { SearchRun } from "./SearchRun"

// jsdom has no layout, Web Animations, matchMedia or ResizeObserver; the virtualizer reads
// offset sizes. Reduced motion: evidence mounts and unmounts at once.
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 800 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 1200 })
  HTMLElement.prototype.animate = vi.fn()
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: true,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {
        // Layout is stubbed; nothing to observe.
      }
      unobserve() {
        // See observe.
      }
      disconnect() {
        // See observe.
      }
    },
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const rowNames = () =>
  [...document.querySelectorAll<HTMLElement>(".result-row .result-who b")].map((name) => name.textContent)

describe("SearchRun", () => {
  it("shows the header, the pond chain and both score tables in the documented order", () => {
    render(<SearchRun payload={RUN} status="awaiting_diagnosis" />)
    const header = within(screen.getByRole("banner"))
    expect(header.getByRole("heading", { name: "Backend Engineer" })).toBeTruthy()
    expect(header.getByText("Search complete")).toBeTruthy()
    expect(header.getByText("6 people")).toBeTruthy()
    expect(header.getByText("$1.25")).toBeTruthy()
    expect([...document.querySelectorAll("[data-pond] .pond-count")].map((pond) => pond.textContent)).toEqual(
      ["Kept 3 of 40", "Kept 3 of 80"],
    )
    expect(screen.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Jev qualification scores",
      "Rating-based scores",
    ])
    expect(rowNames()).toEqual(RANKED_NAMES.flat())
  })

  it("shows scores, missing scores as words, signals and sources", () => {
    render(<SearchRun payload={RUN} />)
    const jordan = within(document.querySelector<HTMLElement>("[data-person-id='p-jordan']") ?? document.body)
    expect(jordan.getByText("5/5")).toBeTruthy()
    expect(jordan.getByText("Suggested pin")).toBeTruthy()
    expect(jordan.getByText("Taste 4.5")).toBeTruthy()
    expect(jordan.getByText("Team #2")).toBeTruthy()
    expect(jordan.getAllByRole("img").map((icon) => icon.getAttribute("aria-label"))).toEqual([
      "Gmail",
      "LinkedIn",
    ])
    expect(jordan.getByText("DK")).toBeTruthy()
    const casey = within(document.querySelector<HTMLElement>("[data-person-id='p-casey']") ?? document.body)
    expect(casey.getByText("Not judged")).toBeTruthy()
    expect(casey.queryByText(/\/5/)).toBeNull()
  })

  it("expands a row in place to its evidence; each row on its own", () => {
    render(<SearchRun payload={RUN} />)
    const jordan = screen.getByRole("button", { name: /Jordan Bravo/ })
    fireEvent.click(jordan)
    expect(jordan.getAttribute("aria-expanded")).toBe("true")
    const evidence = within(document.querySelector<HTMLElement>("[data-evidence]") ?? document.body)
    expect(evidence.getByText("Built this exact system twice.")).toBeTruthy()
    expect(evidence.getByText("Backend depth:")).toBeTruthy()
    expect(evidence.getByText("Matched")).toBeTruthy()
    expect(evidence.getByText("Example University")).toBeTruthy()
    expect(evidence.getByRole("link", { name: "Open on LinkedIn" }).getAttribute("href")).toBe(
      "https://www.linkedin.com/in/jordan-bravo",
    )
    fireEvent.click(screen.getByRole("button", { name: /Casey Delta/ }))
    expect(document.querySelectorAll("[data-evidence]")).toHaveLength(2)
    fireEvent.click(jordan)
    expect(jordan.getAttribute("aria-expanded")).toBe("false")
    expect(document.querySelectorAll("[data-evidence]")).toHaveLength(1)
  })

  it("fills the slots: toolbar, header actions, a row's actions and its tags", () => {
    render(
      <SearchRun
        payload={RUN}
        toolbar={<span>Toolbar slot</span>}
        headerActions={<button type="button">Send feedback</button>}
        rowActions={(candidate) => <button type="button">Score {candidate.name}</button>}
        tags={{ tags: ["Pinned"], assignments: { "p-riley": ["Pinned"] } }}
      />,
    )
    expect(screen.getByText("Toolbar slot")).toBeTruthy()
    expect(within(screen.getByRole("banner")).getByRole("button", { name: "Send feedback" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Score Avery Golf" })).toBeTruthy()
    const riley = document.querySelector("[data-person-id='p-riley'] [data-tag='Pinned']")
    expect(riley?.textContent).toBe("Pinned")
  })

  it("folds the team open", () => {
    render(<SearchRun payload={RUN} />)
    expect(screen.queryByText("Sam India")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: /^Team 1/ }))
    expect(screen.getByText("Sam India")).toBeTruthy()
  })
})
