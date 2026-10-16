import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import type { PreparedSearch } from "@/lib/agent/preparedSearch"

import { SearchCard } from "./SearchCard"

const store = vi.hoisted(() => ({ send: vi.fn() }))
vi.mock("@/lib/agent/store", () => store)

afterEach(() => {
  cleanup()
  store.send.mockReset()
})

const SEARCH: PreparedSearch = {
  query: "VP of finance in New York",
  filters: [
    { key: "metro_areas", label: "Metro areas", value: "New York Metropolitan Area" },
    { key: "seniority_bands", label: "Seniority bands", value: "vice-president, director" },
    { key: "is_current_role", label: "Is current role", value: "true" },
  ],
  traits: [
    { value: "VP of Finance", temporal: "current" },
    { value: "investment banking", temporal: "past" },
  ],
  pool: null,
  notes: [],
  blockers: [],
}

function items(name: string): (string | null)[] {
  return within(screen.getByRole("list", { name }))
    .getAllByRole("listitem")
    .map((item) => item.textContent)
}

function fields(name: string): string[] {
  return within(screen.getByRole("list", { name }))
    .getAllByRole<HTMLInputElement>("textbox")
    .map((field) => field.value)
}

it("shows the traits beside the filters, read-only on a card that no longer asks", () => {
  render(<SearchCard search={SEARCH} actions={false} />)
  expect(items("Traits")).toEqual(["VP of Finance (current)", "investment banking (past)"])
  expect(items("Filters")).toEqual([
    "Metro areas: New York Metropolitan Area",
    "Seniority bands: vice-president, director",
    "Is current role: true",
  ])
  expect(screen.queryByRole("textbox")).toBeNull()
  expect(screen.queryByRole("button")).toBeNull()
})

it("shows no traits from a prepare that carries none, asking or not", () => {
  const { rerender } = render(<SearchCard search={{ ...SEARCH, traits: [] }} actions={false} />)
  expect(screen.queryByRole("list", { name: "Traits" })).toBeNull()
  rerender(<SearchCard search={{ ...SEARCH, traits: [] }} actions={true} />)
  expect(screen.queryByRole("list", { name: "Traits" })).toBeNull()
  expect(fields("Filters")).toEqual(["New York Metropolitan Area", "vice-president, director", "true"])
})

it("edits the traits and the filters in place on the asking card", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  expect(fields("Traits")).toEqual(["VP of Finance", "investment banking"])
  expect(fields("Filters")).toEqual(["New York Metropolitan Area", "vice-president, director", "true"])
  expect(screen.getByRole("textbox", { name: "Add trait" })).toBeTruthy()
  expect(screen.getByRole("button", { name: "Remove Metro areas" })).toBeTruthy()
  expect(screen.getByRole("button", { name: "Remove VP of Finance" })).toBeTruthy()
})

it("sends execute when nothing changed, an edit put back included", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  const metro = screen.getByRole("textbox", { name: "Metro areas" })
  fireEvent.change(metro, { target: { value: "New York City" } })
  fireEvent.change(metro, { target: { value: "New York Metropolitan Area" } })
  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith("execute")
})

it("sends every filter and trait after an edited value, a removed filter and an added trait", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Metro areas" }), {
    target: { value: "New York City, Boston" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Remove Seniority bands" }))
  const add = screen.getByRole("textbox", { name: "Add trait" })
  fireEvent.change(add, { target: { value: "CFO" } })
  fireEvent.keyDown(add, { key: "Enter" })
  expect(fields("Traits")).toEqual(["VP of Finance", "investment banking", "CFO"])
  expect(fields("Filters")).toEqual(["New York City, Boston", "true"])

  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith(
    "Modify, then execute:\n" +
      "filters: metro_areas = New York City, Boston; seniority_bands =; is_current_role = true\n" +
      "traits: VP of Finance (current); investment banking (past); CFO (current)",
  )
})

it("sends no traits line for a search that came with none, after a filter edit", () => {
  render(<SearchCard search={{ ...SEARCH, traits: [] }} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Metro areas" }), {
    target: { value: "New York City" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith(
    "Modify, then execute:\n" +
      "filters: metro_areas = New York City; seniority_bands = vice-president, director; is_current_role = true",
  )
})

it("sends an empty traits line when every trait the card showed is removed", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove VP of Finance" }))
  fireEvent.click(screen.getByRole("button", { name: "Remove investment banking" }))
  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith(
    "Modify, then execute:\n" +
      "filters: metro_areas = New York Metropolitan Area; seniority_bands = vice-president, director; is_current_role = true\n" +
      "traits:",
  )
})

it("sends a trait typed into Add trait without an Enter", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Add trait" }), { target: { value: " CFO " } })
  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith(
    "Modify, then execute:\n" +
      "filters: metro_areas = New York Metropolitan Area; seniority_bands = vice-president, director; is_current_role = true\n" +
      "traits: VP of Finance (current); investment banking (past); CFO (current)",
  )
})

it("sends a removed trait's absence from the traits line", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove investment banking" }))
  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
  expect(store.send).toHaveBeenCalledWith(
    "Modify, then execute:\n" +
      "filters: metro_areas = New York Metropolitan Area; seniority_bands = vice-president, director; is_current_role = true\n" +
      "traits: VP of Finance (current)",
  )
})

it("edits a blocked card but never executes it", () => {
  render(<SearchCard search={{ ...SEARCH, blockers: ["bm25_queries is empty"] }} actions={true} />)
  expect(fields("Filters")).toEqual(["New York Metropolitan Area", "vice-president, director", "true"])
  expect(screen.queryByRole("button", { name: "Execute" })).toBeNull()
  expect(screen.getByRole("button", { name: "Modify" })).toBeTruthy()
})
