import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"

// The modal's scroll area watches element sizes; jsdom has no ResizeObserver.
beforeAll(() => {
  if (!("ResizeObserver" in globalThis))
    Object.defineProperty(globalThis, "ResizeObserver", {
      value: class {
        observe = () => undefined
        unobserve = () => undefined
        disconnect = () => undefined
      },
    })
})

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

it("sends the query, the changed filters and the traits as JSON after edits", () => {
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
    // Unchanged keys (years_experience_min) stay out: a key sent is an edit to the agent.
    filters: {
      metro_areas: ["Washington, DC"],
      seniority_bands: ["vice-president", "c-suite"],
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
  // The traits did not change, so none are sent: to the agent, traits sent mean a new search.
  expect(sent()).toEqual({ filters: { metro_areas: null, years_experience_min: null } })
})

it("adds a filter the search did not come with through the Edit Filters modal", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  expect(screen.queryByText("Companies")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "More filters…" }))
  const dialog = screen.getByRole("dialog", { name: "Edit Filters" })
  // The modal opens on the prepared search: the seniority picker already lists its two bands.
  expect(within(dialog).getByText("Vice President, Director")).toBeTruthy()
  fireEvent.click(within(dialog).getByRole("button", { name: /^Company/ }))
  const company = within(dialog).getByPlaceholderText("e.g., Google")
  fireEvent.change(company, { target: { value: "Stripe" } })
  fireEvent.keyDown(company, { key: "Enter" })
  fireEvent.click(within(dialog).getByRole("button", { name: "Apply Filters" }))
  // Applied: the card's preview layer shows the company beside the prepared rows.
  expect(screen.getByText("Companies")).toBeTruthy()
  expect(screen.getByRole<HTMLInputElement>("textbox", { name: "Companies 1" }).value).toBe("Stripe")
  execute()
  expect(sent().filters).toEqual({ company_names: ["Stripe"] })
})

it("clears both ends of a range from the modal, and keeps a typed location under the kind picked", () => {
  const search: PreparedSearch = {
    ...SEARCH,
    filters: [...SEARCH.filters, { key: "headcount_min", value: 50 }, { key: "headcount_max", value: 500 }],
  }
  render(<SearchCard search={search} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "More filters…" }))
  const dialog = screen.getByRole("dialog", { name: "Edit Filters" })
  fireEvent.click(within(dialog).getByRole("button", { name: /^Company/ }))
  // Headcount's clear fires two updates in one click; both must land.
  const headcount = within(dialog).getByText("Headcount").parentElement
  if (!headcount) throw new Error("no headcount row")
  fireEvent.click(within(headcount).getByRole("button", { name: "clear" }))
  fireEvent.click(within(dialog).getByRole("button", { name: /^Location/ }))
  const place = within(dialog).getByPlaceholderText("e.g., San Francisco, California")
  fireEvent.change(place, { target: { value: "Brooklyn" } })
  fireEvent.keyDown(place, { key: "Enter" })
  fireEvent.click(within(dialog).getByRole("button", { name: "Apply Filters" }))
  execute()
  const { filters } = sent()
  expect(filters.headcount_min).toBeNull()
  expect(filters.headcount_max).toBeNull()
  // The default kind is metro area, so the typed name joins the prepared one.
  expect(filters.metro_areas).toEqual(["New York Metropolitan Area", "Brooklyn"])
})

it("drops the modal's edits on Cancel", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "More filters…" }))
  const dialog = screen.getByRole("dialog", { name: "Edit Filters" })
  fireEvent.click(within(dialog).getByRole("button", { name: /^Company/ }))
  const company = within(dialog).getByPlaceholderText("e.g., Google")
  fireEvent.change(company, { target: { value: "Stripe" } })
  fireEvent.keyDown(company, { key: "Enter" })
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }))
  expect(screen.queryByText("Companies")).toBeNull()
  execute()
  expect(store.send).toHaveBeenCalledWith("execute")
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
  expect(sent()).toEqual({ filters: { seniority_bands: ["vice-president"] } })
})

it("sends a changed end of a select pair, the other end untouched", () => {
  const search: PreparedSearch = {
    ...SEARCH,
    filters: [...SEARCH.filters, { key: "funding_stage_min", value: "seed" }],
  }
  render(<SearchCard search={search} actions={true} />)
  fireEvent.change(screen.getByRole("combobox", { name: "Stage max" }), { target: { value: "series_b" } })
  execute()
  expect(sent().filters).toEqual({ funding_stage_max: "series_b" })
})

it("sends an empty traits list when every trait is removed", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove VP of Finance" }))
  fireEvent.click(screen.getByRole("button", { name: "Remove investment banking" }))
  execute()
  expect(sent().traits).toEqual([])
})

it("keeps showing what it sent once the card no longer asks", () => {
  const { rerender } = render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Query" }), { target: { value: "CFOs in NYC" } })
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Add trait" }), { target: { value: "CFO" } })
  execute()
  rerender(<SearchCard search={SEARCH} actions={false} />)
  expect(screen.getByRole("heading", { name: "CFOs in NYC" })).toBeTruthy()
  expect(screen.queryByText("director")).toBeNull()
  expect(screen.getByText("CFO").textContent).toContain("(current)")
  expect(screen.queryByRole("button")).toBeNull()
})

it("applies the kind chosen after the location was typed", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  const add = screen.getByRole("textbox", { name: "Add Location" })
  const kind = screen.getByRole("combobox", { name: "Location kind" })
  fireEvent.change(add, { target: { value: "Brooklyn" } })
  fireEvent.blur(add, { relatedTarget: kind })
  fireEvent.change(kind, { target: { value: "cities" } })
  // No Enter: Execute carries what sits in the add field, under the kind picked after typing.
  execute()
  expect(sent().filters).toEqual({ cities: ["Brooklyn"] })
})

it("shows a filter the table doesn't name with its value's own shape and keeps its type", () => {
  const search: PreparedSearch = {
    ...SEARCH,
    filters: [
      ...SEARCH.filters,
      { key: "custom_score_min", value: 3 },
      { key: "remote_ok", value: true },
      { key: "team_note", value: "platform" },
    ],
  }
  render(<SearchCard search={search} actions={true} />)
  const score = screen.getByRole("textbox", { name: "Custom score min" })
  fireEvent.change(score, { target: { value: "" } })
  fireEvent.change(score, { target: { value: "4" } })
  expect(screen.getByRole("checkbox", { name: "Remote ok" })).toBeTruthy()
  fireEvent.change(screen.getByRole("textbox", { name: "Team note" }), { target: { value: "infra" } })
  execute()
  // The number stays a number; the untouched toggle is not sent.
  expect(sent().filters).toEqual({ custom_score_min: 4, team_note: "infra" })
})

it("shows the current-only toggle only to a search without traits", () => {
  const current: PreparedSearch = {
    ...SEARCH,
    traits: [],
    filters: [...SEARCH.filters, { key: "is_current_role", value: true }],
  }
  const { rerender } = render(<SearchCard search={current} actions={true} />)
  expect(screen.getByRole("checkbox", { name: "Current role only" })).toBeTruthy()
  rerender(<SearchCard search={{ ...current, traits: SEARCH.traits }} actions={true} />)
  expect(screen.queryByRole("checkbox", { name: "Current role only" })).toBeNull()
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
