import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { CARDS, RUN_ID } from "@/testing/searches-fixture"

import { CatalogList } from "./CatalogList"

// jsdom has no Web Animations, matchMedia or scrollIntoView; reduced motion skips the entrance.
beforeEach(() => {
  HTMLElement.prototype.animate = vi.fn()
  HTMLElement.prototype.scrollIntoView = vi.fn()
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

const runIds = () => screen.getAllByRole("button", { name: /people|person/ }).map((row) => row.dataset.runId)

describe("CatalogList", () => {
  it("preselects the newest version, offers All but never an unversioned chip, and counts", () => {
    render(<CatalogList cards={CARDS} selectedId={null} onOpen={vi.fn()} />)
    const versions = within(screen.getByRole("group", { name: "Search version" }))
    expect(versions.getAllByRole("button").map((chip) => chip.textContent)).toEqual([
      "2026-09-26",
      "2026-09-01",
      "All",
    ])
    expect(versions.getByRole("button", { name: "2026-09-26" }).getAttribute("aria-pressed")).toBe("true")
    expect(runIds()).toEqual([RUN_ID, "casey-role"])
    expect(screen.getByText("2 of 4 searches")).toBeTruthy()
    fireEvent.click(versions.getByRole("button", { name: "All" }))
    expect(runIds()).toEqual([RUN_ID, "casey-role", "morgan-role", "riley-role"])
  })

  it("says who is pinned and how many scored 5, 4 and 3 on the people count", () => {
    render(<CatalogList cards={CARDS} selectedId={null} onOpen={vi.fn()} />)
    const row = document.querySelector(`[data-run-id='${RUN_ID}'] [data-people-counts]`)
    expect(row?.getAttribute("title")).toBe("Pinned 0 · Overall 5/5: 1, 4/5: 1, 3/5: 0")
  })

  it("filters as you type and says when nothing matches", () => {
    render(<CatalogList cards={CARDS} selectedId={null} onOpen={vi.fn()} />)
    const search = screen.getByRole("searchbox", { name: "Search saved searches" })
    fireEvent.change(search, { target: { value: "design" } })
    expect(runIds()).toEqual(["casey-role"])
    fireEvent.change(search, { target: { value: "nobody" } })
    expect(screen.getByText("No searches match")).toBeTruthy()
    expect(screen.getByText("0 of 4 searches")).toBeTruthy()
  })

  it("marks the open run, and arrows then Enter open the next one", () => {
    const onOpen = vi.fn()
    render(<CatalogList cards={CARDS} selectedId={RUN_ID} onOpen={onOpen} />)
    const open = screen.getAllByRole("button", { current: "page" })
    expect(open.map((row) => row.dataset.runId)).toEqual([RUN_ID])
    fireEvent.keyDown(document.body, { key: "ArrowDown" })
    fireEvent.keyDown(document.body, { key: "Enter" })
    expect(onOpen).toHaveBeenCalledWith("casey-role")
    fireEvent.click(screen.getByRole("button", { name: /Backend Engineer/ }))
    expect(onOpen).toHaveBeenLastCalledWith(RUN_ID)
  })

  it("leaves j/k and Enter on the open run to the results", () => {
    const onOpen = vi.fn()
    render(<CatalogList cards={CARDS} selectedId={RUN_ID} onOpen={onOpen} />)
    fireEvent.keyDown(document.body, { key: "j" })
    fireEvent.keyDown(document.body, { key: "Enter" })
    expect(onOpen).not.toHaveBeenCalled()
  })

  it("keeps the filters for the tab: a return to Searches finds them as they were", () => {
    const { unmount } = render(<CatalogList cards={CARDS} selectedId={null} onOpen={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "All" }))
    fireEvent.change(screen.getByRole("searchbox", { name: "Search saved searches" }), {
      target: { value: "sample" },
    })
    unmount()
    render(<CatalogList cards={CARDS} selectedId={null} onOpen={vi.fn()} />)
    expect(screen.getByRole("searchbox", { name: "Search saved searches" })).toHaveProperty("value", "sample")
    expect(runIds()).toEqual(["casey-role", "riley-role"])
  })
})
