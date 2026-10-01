import { cleanup, createEvent, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { worthPending } from "@/testing/review-fixture"
import type { WorthPendingEntry } from "@/types/review"

import { WorthSearch } from "./WorthSearch"

afterEach(cleanup)

function renderSearch(names: WorthPendingEntry[] = worthPending()) {
  const onPick = vi.fn()
  const view = render(<WorthSearch names={names} onPick={onPick} />)
  const input = screen.getByRole<HTMLInputElement>("searchbox", { name: "Search people by name" })
  const list = screen.getByRole("listbox", { hidden: true })
  const type = (text: string) => {
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: text } })
  }
  const options = () => [...list.querySelectorAll("li")].map((item) => item.textContent)
  const active = () => [...list.querySelectorAll("li.active")].map((item) => item.textContent)
  /** Presses a key; true when the box took it (the browser's own action is cancelled). */
  const press = (key: string) => !fireEvent.keyDown(input, { key })
  return { ...view, onPick, input, list, type, options, active, press }
}

/** Twelve pending people who all match "ali". */
function manyNames(): WorthPendingEntry[] {
  return Array.from({ length: 12 }, (_, position) => ({
    key: `worth-ali-${position}`,
    name: `Ali Sample ${position}`,
  }))
}

// W11
describe("WorthSearch", () => {
  it("is a search box with a closed list", () => {
    const { input, list, container } = renderSearch()
    expect(container.firstElementChild?.className).toBe("worth-search")
    expect(input.className).toBe("worth-search-input")
    expect(input.type).toBe("search")
    expect(input.placeholder).toBe("Search people…")
    expect(input.getAttribute("autocomplete")).toBe("off")
    expect(input.getAttribute("spellcheck")).toBe("false")
    expect(list.className).toBe("worth-search-list")
    expect(list.hidden).toBe(true)
  })

  it("lists the pending people whose name holds what is typed, whatever its case", () => {
    const { type, options, list } = renderSearch()
    type("E")
    expect(list.hidden).toBe(false)
    expect(options()).toEqual(["Casey Delta", "Riley Echo"])
    type("  RDAN ")
    expect(options()).toEqual(["Jordan Bravo"])
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["Jordan Bravo"])
  })

  it("lists at most eight, the first highlighted", () => {
    const { type, options, active } = renderSearch(manyNames())
    type("ali")
    expect(options()).toHaveLength(8)
    expect(options()[7]).toBe("Ali Sample 7")
    expect(active()).toEqual(["Ali Sample 0"])
    expect(screen.getAllByRole("option").map((option) => option.getAttribute("aria-selected"))).toEqual([
      "true",
      ...Array.from({ length: 7 }, () => "false"),
    ])
  })

  it("says so when nobody matches", () => {
    const { type, list, options, active } = renderSearch()
    type("zz")
    expect(list.hidden).toBe(false)
    expect(options()).toEqual(["No matches"])
    expect(list.querySelector("li")?.className).toBe("worth-search-empty")
    expect(screen.queryAllByRole("option")).toEqual([])
    expect(active()).toEqual([])
  })

  it("shows no list for an empty or blank box", () => {
    const { type, list, options } = renderSearch()
    type("   ")
    expect(list.hidden).toBe(true)
    expect(options()).toEqual([])
    type("e")
    type("")
    expect(list.hidden).toBe(true)
  })

  it("moves the highlight with the arrows, round both ends", () => {
    const { type, active, press } = renderSearch()
    type("e")
    expect(active()).toEqual(["Casey Delta"])
    expect(press("ArrowDown")).toBe(true)
    expect(active()).toEqual(["Riley Echo"])
    press("ArrowDown")
    expect(active()).toEqual(["Casey Delta"])
    expect(press("ArrowUp")).toBe(true)
    expect(active()).toEqual(["Riley Echo"])
  })

  it("highlights the first again when the text changes", () => {
    const { type, active, press } = renderSearch()
    type("e")
    press("ArrowDown")
    type("ey")
    expect(active()).toEqual(["Casey Delta"])
  })

  it("picks the highlighted person on Enter, clearing and closing the box", () => {
    const { type, press, onPick, input, list } = renderSearch()
    type("e")
    press("ArrowDown")
    expect(press("Enter")).toBe(true)
    expect(onPick.mock.calls).toEqual([["worth-riley"]])
    expect(input.value).toBe("")
    expect(list.hidden).toBe(true)
  })

  it("picks nobody on Enter when nobody matches or the list is closed", () => {
    const { type, press, onPick, input } = renderSearch()
    expect(press("Enter")).toBe(true)
    type("zz")
    press("Enter")
    expect(onPick).not.toHaveBeenCalled()
    expect(input.value).toBe("zz")
  })

  it("clears the box on Escape", () => {
    const { type, press, input, list, onPick } = renderSearch()
    type("e")
    expect(press("Escape")).toBe(true)
    expect(input.value).toBe("")
    expect(list.hidden).toBe(true)
    expect(onPick).not.toHaveBeenCalled()
  })

  it("closes the list on blur, keeps the text, and opens it again on focus", () => {
    const { type, input, list, options } = renderSearch()
    type("e")
    fireEvent.blur(input)
    expect(list.hidden).toBe(true)
    expect(input.value).toBe("e")
    fireEvent.focus(input)
    expect(list.hidden).toBe(false)
    expect(options()).toEqual(["Casey Delta", "Riley Echo"])
  })

  it("picks on mousedown, before the box can blur", () => {
    const { type, onPick, input, list } = renderSearch()
    type("e")
    const option = screen.getByRole("option", { name: "Riley Echo" })
    const press = createEvent.mouseDown(option)
    fireEvent(option, press)
    expect(press.defaultPrevented).toBe(true)
    expect(onPick.mock.calls).toEqual([["worth-riley"]])
    expect(input.value).toBe("")
    expect(list.hidden).toBe(true)
  })

  it("leaves other keys to the box", () => {
    const { type, press } = renderSearch()
    type("e")
    expect(press("a")).toBe(false)
  })

  it("drops a name from the open list when the person is decided", () => {
    const { type, options, rerender, onPick } = renderSearch()
    type("e")
    rerender(<WorthSearch names={worthPending().slice(1)} onPick={onPick} />)
    expect(options()).toEqual(["Riley Echo"])
  })
})
