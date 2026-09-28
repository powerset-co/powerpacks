import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { exportScoreOf } from "@/lib/searches/feedback"
import { filterRows, NO_FILTERS, type ResultFilters } from "@/lib/searches/filters"
import { NO_TAGS } from "@/lib/searches/tags"
import { operator, resultRow } from "@/testing/searches-fixture"
import type { Tagged } from "@/types/searches"

import { ResultsToolbar, type ResultsToolbarProps } from "./ResultsToolbar"

const ROWS = [
  resultRow("p-jordan", "Jordan Bravo", { overall: 5, operators: [operator("op-1", "Drew Kilo")] }),
  resultRow("p-casey", "Casey Delta", { overall: 4 }),
  resultRow("p-morgan", "Morgan Echo", { overall: 2 }),
]
const TAGGED: Tagged = { tags: ["Backend", "Infra"], assignments: { "p-jordan": ["Backend"] } }

function toolbar(props: Partial<ResultsToolbarProps> = {}) {
  const handlers = {
    onFiltersChange: vi.fn<(filters: ResultFilters) => void>(),
    onUntag: vi.fn<(ids: readonly string[]) => void>(),
    onClearTags: vi.fn<() => void>(),
    onAnnounce: vi.fn(),
  }
  const filters = props.filters ?? NO_FILTERS
  const tagged = props.tagged ?? TAGGED
  render(
    <ResultsToolbar
      title="Backend Engineer"
      rows={ROWS}
      shown={filterRows(ROWS, filters, tagged)}
      scored
      tagged={tagged}
      filters={filters}
      exportScore={exportScoreOf(new Map())}
      {...handlers}
      {...props}
    />,
  )
  return handlers
}

function lastFilters(handler: ReturnType<typeof toolbar>["onFiltersChange"]): ResultFilters | undefined {
  return handler.mock.calls.at(-1)?.[0]
}

// Reduced motion: presence mounts and unmounts at once, as in the People suites.
beforeEach(() => {
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: true,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe("ResultsToolbar", () => {
  it("shows the tagged count and turns tagged only on", () => {
    const { onFiltersChange } = toolbar()
    fireEvent.click(screen.getByRole("button", { name: "Tagged (1)" }))
    expect(lastFilters(onFiltersChange)?.taggedOnly).toBe(true)
  })

  it("hides Tagged when nobody is tagged", () => {
    toolbar({ tagged: NO_TAGS })
    expect(screen.queryByRole("button", { name: /Tagged/ })).toBeNull()
  })

  it("toggles labels and remembers it for the tab", () => {
    const { onFiltersChange } = toolbar()
    fireEvent.click(screen.getByRole("button", { name: "Labels", pressed: true }))
    expect(lastFilters(onFiltersChange)?.labels).toBe(false)
    expect(sessionStorage.getItem("powerpacks:search-labels")).toBe("false")
  })

  it("adds overall scores and All scores clears them", () => {
    const { onFiltersChange } = toolbar({ filters: { ...NO_FILTERS, scores: new Set([4]) } })
    expect(screen.getByRole("button", { name: "Overall score 4" }).getAttribute("aria-pressed")).toBe("true")
    fireEvent.click(screen.getByRole("button", { name: "Overall score 5" }))
    expect(lastFilters(onFiltersChange)?.scores).toEqual(new Set([4, 5]))
    fireEvent.click(screen.getByRole("button", { name: "All scores" }))
    expect(lastFilters(onFiltersChange)?.scores).toEqual(new Set())
  })

  it("has no score filter on a pond table", () => {
    toolbar({ scored: false })
    expect(screen.queryByRole("group", { name: "Overall score filter" })).toBeNull()
  })

  it("picks an operator from the list and removes it from its chip", () => {
    const { onFiltersChange } = toolbar()
    fireEvent.click(screen.getByRole("button", { name: "Add operator" }))
    fireEvent.click(screen.getByRole("checkbox", { name: /Drew Kilo/ }))
    expect(lastFilters(onFiltersChange)?.operators).toEqual(new Set(["op-1"]))
    fireEvent.keyDown(document, { key: "Escape" })
    expect(screen.queryByRole("group", { name: "Choose operators" })).toBeNull()

    cleanup()
    const next = toolbar({ filters: { ...NO_FILTERS, operators: new Set(["op-1"]) } })
    fireEvent.click(screen.getByRole("button", { name: "Remove Drew Kilo" }))
    expect(lastFilters(next.onFiltersChange)?.operators).toEqual(new Set())
  })

  it("filters by tag within tagged only, and clears the tag filter", () => {
    const { onFiltersChange } = toolbar({ filters: { ...NO_FILTERS, taggedOnly: true } })
    fireEvent.click(screen.getByRole("button", { name: "Infra" }))
    expect(lastFilters(onFiltersChange)?.tags).toEqual(new Set(["Infra"]))

    cleanup()
    const next = toolbar({ filters: { ...NO_FILTERS, taggedOnly: true, tags: new Set(["Backend"]) } })
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }))
    expect(lastFilters(next.onFiltersChange)?.tags).toEqual(new Set())
  })

  it("counts what the filters keep", () => {
    toolbar({ filters: { ...NO_FILTERS, scores: new Set([4, 5]) } })
    expect(screen.getByRole("status").textContent).toBe("2 of 3 results")
  })

  it("untags everyone shown, and clears all tags only after Confirm", () => {
    const { onUntag, onClearTags } = toolbar({ filters: { ...NO_FILTERS, taggedOnly: true } })
    fireEvent.click(screen.getByRole("button", { name: "Untag all on page" }))
    expect(onUntag).toHaveBeenCalledWith(["p-jordan"])
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }))
    expect(onClearTags).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }))
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }))
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }))
    expect(onClearTags).toHaveBeenCalledOnce()
  })

  it("disables Copy and CSV when nothing matches", () => {
    toolbar({ filters: { ...NO_FILTERS, scores: new Set([1]) } })
    expect(screen.getByRole("button", { name: "Copy" })).toHaveProperty("disabled", true)
    expect(screen.getByRole("button", { name: "CSV" })).toHaveProperty("disabled", true)
  })

  it("copies every matching row and says how many", async () => {
    const write = vi.fn(() => Promise.resolve())
    vi.stubGlobal("navigator", { clipboard: { write } })
    vi.stubGlobal(
      "ClipboardItem",
      class {
        constructor(readonly items: Record<string, Blob>) {}
      },
    )
    const { onAnnounce } = toolbar()
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await vi.waitFor(() => expect(onAnnounce).toHaveBeenCalledWith({ message: "Copied 3 results." }))
  })

  it("downloads a CSV named for the exported people's tags", () => {
    vi.stubGlobal("URL", { createObjectURL: () => "blob:csv", revokeObjectURL: vi.fn() })
    const names: string[] = []
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      names.push(this.download)
    })
    const { onAnnounce } = toolbar({ filters: { ...NO_FILTERS, taggedOnly: true } })
    fireEvent.click(screen.getByRole("button", { name: "CSV" }))
    expect(names[0]).toMatch(/^backend_backend-engineer_\d{4}-\d{2}-\d{2}\.csv$/)
    expect(onAnnounce).toHaveBeenCalledWith({ message: "Exported 1 result." })
    click.mockRestore()
  })
})
