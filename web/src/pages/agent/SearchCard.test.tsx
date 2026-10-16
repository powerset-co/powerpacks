import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import type { PreparedSearch } from "@/lib/agent/preparedSearch"
import { isRecord } from "@/lib/utils"

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
    { key: "metro_areas", value: ["New York Metropolitan Area"] },
    { key: "seniority_bands", value: ["vice-president", "director"] },
    { key: "years_experience_min", value: 5 },
    { key: "bm25_queries", value: ["vp finance"] },
  ],
  traits: [
    { value: "VP of Finance", temporal: "current", meaning: "role" },
    { value: "investment banking", temporal: "past", meaning: "experience" },
  ],
  pool: null,
  notes: [],
  blockers: [],
}

/** The one message the card sent. */
function message(): string {
  const [first] = store.send.mock.calls[0] ?? []
  if (typeof first !== "string") throw new Error("nothing sent")
  return first
}

/** The message Execute sent, parsed: the JSON after "Modify, then execute:". */
function sent(): { query?: string; filters: Record<string, unknown>; traits?: unknown[] } {
  const text = message()
  expect(text.startsWith("Modify, then execute:\n")).toBe(true)
  const parsed: unknown = JSON.parse(text.slice("Modify, then execute:\n".length))
  if (!isRecord(parsed) || !isRecord(parsed.filters)) throw new Error("no filters in the message")
  return {
    query: typeof parsed.query === "string" ? parsed.query : undefined,
    filters: parsed.filters,
    traits: Array.isArray(parsed.traits) ? parsed.traits : undefined,
  }
}

function execute() {
  fireEvent.click(screen.getByRole("button", { name: "Execute" }))
}

it("shows the query, the traits and the filled filters read-only on a card that no longer asks", () => {
  render(<SearchCard search={SEARCH} actions={false} />)
  expect(screen.getByRole("heading", { name: "VP of finance in New York" })).toBeTruthy()
  expect(screen.getByText("VP of Finance").textContent).toContain("(current)")
  expect(screen.getByText("New York Metropolitan Area").textContent).toContain("metro")
  expect(screen.getByText("vice-president")).toBeTruthy()
  expect(screen.getByText("5 – any")).toBeTruthy()
  expect(screen.getByText("vp finance")).toBeTruthy()
  expect(screen.queryByText("Companies")).toBeNull()
  expect(screen.queryByRole("textbox")).toBeNull()
  expect(screen.queryByRole("button")).toBeNull()
})

it("sends execute when nothing changed, an edit put back included", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  const query = screen.getByRole("textbox", { name: "Query" })
  fireEvent.change(query, { target: { value: "CFOs in New York" } })
  expect(screen.getByRole("button", { name: "Reset" })).toBeTruthy()
  fireEvent.change(query, { target: { value: "VP of finance in New York" } })
  expect(screen.queryByRole("button", { name: "Reset" })).toBeNull()
  execute()
  expect(store.send).toHaveBeenCalledWith("execute")
})

it("sends the query, every filter and every trait as JSON after edits", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Query" }), {
    target: { value: "Finance leaders in NYC" },
  })
  // A location with a comma inside stays one name.
  fireEvent.change(screen.getByRole("textbox", { name: "Location 1" }), {
    target: { value: "Washington, DC" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.change(screen.getByRole("combobox", { name: "Add Seniority" }), { target: { value: "c-suite" } })
  fireEvent.change(screen.getByRole("spinbutton", { name: "Years max" }), { target: { value: "12" } })
  const keyword = screen.getByRole("textbox", { name: "Add Keywords" })
  fireEvent.change(keyword, { target: { value: "head of finance" } })
  fireEvent.keyDown(keyword, { key: "Enter" })
  fireEvent.click(screen.getByRole("button", { name: "investment banking: past" }))
  const trait = screen.getByRole("textbox", { name: "Add trait" })
  fireEvent.change(trait, { target: { value: "CFO" } })
  execute()
  expect(sent()).toEqual({
    query: "Finance leaders in NYC",
    filters: {
      metro_areas: ["Washington, DC"],
      seniority_bands: ["vice-president", "c-suite"],
      years_experience_min: 5,
      bm25_queries: ["vp finance", "head of finance"],
      years_experience_max: 12,
    },
    traits: [
      { value: "VP of Finance", temporal: "current", meaning: "role" },
      { value: "investment banking", temporal: "all", meaning: "experience" },
      { value: "CFO", temporal: "current", meaning: "role" },
    ],
  })
})

it("sends a removed filter as null and an emptied list as null", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove New York Metropolitan Area" }))
  fireEvent.change(screen.getByRole("spinbutton", { name: "Years min" }), { target: { value: "" } })
  execute()
  expect(sent()).toEqual({
    filters: {
      metro_areas: null,
      seniority_bands: ["vice-president", "director"],
      years_experience_min: null,
      bm25_queries: ["vp finance"],
    },
    traits: SEARCH.traits,
  })
})

it("adds a filter the search did not come with from More filters", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  expect(screen.queryByText("Companies")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "More filters ▾" }))
  expect(screen.getByRole("heading", { name: "Company" })).toBeTruthy()
  // Id lists with nothing in them and the current-only toggles (the traits carry that) are not offered.
  expect(screen.queryByText("Role ids")).toBeNull()
  expect(screen.queryByRole("checkbox", { name: "Current role only" })).toBeNull()
  const company = screen.getByRole("textbox", { name: "Add Companies" })
  fireEvent.change(company, { target: { value: "Stripe" } })
  fireEvent.keyDown(company, { key: "Enter" })
  fireEvent.change(screen.getByRole("combobox", { name: "Stage min" }), { target: { value: "series_b" } })
  fireEvent.change(screen.getByRole("textbox", { name: "Add Location" }), { target: { value: "Brooklyn" } })
  fireEvent.change(screen.getByRole("combobox", { name: "Location kind" }), { target: { value: "cities" } })
  fireEvent.click(screen.getByRole("button", { name: "Fewer filters ▴" }))
  // Closed again: the added fields stay in the preview layer.
  expect(screen.getByText("Companies")).toBeTruthy()
  expect(screen.getByText("Stage")).toBeTruthy()
  execute()
  const body = sent()
  expect(body.filters.company_names).toEqual(["Stripe"])
  expect(body.filters.funding_stage_min).toBe("series_b")
  expect(body.filters.metro_areas).toEqual(["New York Metropolitan Area"])
})

it("keeps a typed location with the chosen kind", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("combobox", { name: "Location kind" }), { target: { value: "cities" } })
  const add = screen.getByRole("textbox", { name: "Add Location" })
  fireEvent.change(add, { target: { value: "Brooklyn" } })
  fireEvent.keyDown(add, { key: "Enter" })
  const chip = screen.getByRole("button", { name: "Remove Brooklyn" }).parentElement
  if (!chip) throw new Error("no chip")
  expect(within(chip).getByText("· city")).toBeTruthy()
  execute()
  const body = sent()
  expect(body.filters.cities).toEqual(["Brooklyn"])
})

it("sends no traits for a search that came with none and still has none", () => {
  render(<SearchCard search={{ ...SEARCH, traits: [] }} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  execute()
  expect(sent()).toEqual({
    filters: {
      metro_areas: ["New York Metropolitan Area"],
      seniority_bands: ["vice-president"],
      years_experience_min: 5,
      bm25_queries: ["vp finance"],
    },
  })
})

it("sends an empty traits list when every trait is removed", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove VP of Finance" }))
  fireEvent.click(screen.getByRole("button", { name: "Remove investment banking" }))
  execute()
  expect(sent().traits).toEqual([])
})

it("resets every edit to the prepared search", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Query" }), { target: { value: "CFOs" } })
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.click(screen.getByRole("button", { name: "Remove VP of Finance" }))
  fireEvent.click(screen.getByRole("button", { name: "Reset" }))
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Query" }).value).toBe(
    "VP of finance in New York",
  )
  expect(screen.getByText("director")).toBeTruthy()
  expect(screen.getByRole("textbox", { name: "Trait 1" })).toBeTruthy()
  execute()
  expect(store.send).toHaveBeenCalledWith("execute")
})

it("sends a blocked card's changes without running it", () => {
  render(<SearchCard search={{ ...SEARCH, blockers: ["bm25_queries is empty"] }} actions={true} />)
  expect(screen.queryByRole("button", { name: "Execute" })).toBeNull()
  const changes = screen.getByRole<HTMLButtonElement>("button", { name: "Send changes" })
  expect(changes.disabled).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.click(changes)
  expect(message().startsWith("Modify:\n")).toBe(true)
  expect(message()).not.toContain("then execute")
})
