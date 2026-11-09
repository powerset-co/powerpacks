import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"

import { pick } from "@/testing/select"

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

const store = vi.hoisted(() => ({ send: vi.fn(), runCommand: vi.fn(), useAgent: () => ({ scope: null }) }))
vi.mock("@/lib/agent/store", () => store)
vi.mock("@/lib/agent/useSearchScope", () => ({ useSearchScope: () => ({ data: undefined }) }))

afterEach(() => {
  cleanup()
  store.send.mockReset()
  store.runCommand.mockReset()
})

const SEARCH: PreparedSearch = {
  query: "VP of finance in New York",
  filters: [
    { key: "metro_areas", value: ["New York Metropolitan Area"] },
    { key: "seniority_bands", value: ["vice-president", "director"] },
    { key: "years_experience_min", value: 5 },
    { key: "bm25_queries", value: ["vp finance"] },
    { key: "role_ids", value: ["finance_lead"] },
  ],
  traits: [
    { value: "VP of Finance", temporal: "current", meaning: "role" },
    { value: "investment banking", temporal: "past", meaning: "experience" },
  ],
  notes: [],
  blockers: [],
  local: false,
  estimate: null,
  run: null,
  runDir: null,
  executeCommand: null,
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
  fireEvent.click(screen.getByRole("button", { name: "Run Search" }))
}

it("folds a card that no longer asks under its query, and opens to the traits and filters read-only", () => {
  render(<SearchCard search={SEARCH} actions={false} />)
  expect(screen.getByRole("heading", { name: "“VP of finance in New York”" })).toBeTruthy()
  expect(screen.getByText("+5 filters")).toBeTruthy()
  expect(screen.queryByText("Search Filters")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "Show filters" }))
  expect(screen.getByText("VP of Finance").parentElement?.textContent).toContain("Current")
  expect(screen.getByText("Metro Areas:")).toBeTruthy()
  expect(screen.getByText("New York Metropolitan Area")).toBeTruthy()
  expect(screen.getByText("Vice-President")).toBeTruthy()
  expect(screen.getByText("Experience:")).toBeTruthy()
  expect(screen.getByText("5+ years")).toBeTruthy()
  expect(screen.getByText("VP Finance")).toBeTruthy()
  // The canonical role ids stay in the search but off the card, as on the cloud app.
  expect(screen.queryByText("Finance Lead")).toBeNull()
  expect(screen.getByText("5 filters applied")).toBeTruthy()
  expect(screen.queryByText("Companies:")).toBeNull()
  expect(screen.queryByRole("textbox")).toBeNull()
  // Only the fold toggle: nothing edits a card that no longer asks.
  expect(screen.getAllByRole("button")).toHaveLength(1)
})

it("shows the estimate the prepare counted", () => {
  render(<SearchCard search={{ ...SEARCH, estimate: 1531 }} actions={true} />)
  expect(screen.getByText("~1,500")).toBeTruthy()
  expect(screen.getByText("estimated results")).toBeTruthy()
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

it("sends a changed query and the traits as a new search, without the filters edited beside them", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Query" }), {
    target: { value: "Finance leaders in NYC" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.click(screen.getByRole("button", { name: "Edit Seniority" }))
  fireEvent.click(screen.getByRole("checkbox", { name: "C-Suite" }))
  fireEvent.change(screen.getByRole("spinbutton", { name: "Years max" }), { target: { value: "12" } })
  fireEvent.click(screen.getByRole("button", { name: "Add Keywords" }))
  const keyword = screen.getByRole("textbox", { name: "Add Keywords" })
  fireEvent.change(keyword, { target: { value: "head of finance" } })
  fireEvent.keyDown(keyword, { key: "Enter" })
  fireEvent.click(screen.getByRole("button", { name: "investment banking: past" }))
  fireEvent.click(screen.getByRole("button", { name: "Add trait" }))
  const trait = screen.getByRole("textbox", { name: "Add trait" })
  fireEvent.change(trait, { target: { value: "CFO" } })
  execute()
  // The filter edits stay out: the next prepare derives its own filters for the new query.
  expect(sent()).toEqual({
    query: "Finance leaders in NYC",
    filters: {},
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
  expect(screen.queryByText("Companies:")).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "Edit Filters" }))
  const dialog = screen.getByRole("dialog", { name: "Edit Filters" })
  // The modal opens on the prepared search: the seniority picker already lists its two bands.
  expect(within(dialog).getByText("Vice President, Director")).toBeTruthy()
  fireEvent.click(within(dialog).getByRole("button", { name: /^Company/ }))
  const company = within(dialog).getByPlaceholderText("e.g., Google")
  fireEvent.change(company, { target: { value: "Stripe" } })
  fireEvent.keyDown(company, { key: "Enter" })
  fireEvent.click(within(dialog).getByRole("button", { name: "Apply Filters" }))
  // Applied: the card's preview layer shows the company beside the prepared rows.
  expect(screen.getByText("Companies:")).toBeTruthy()
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
  fireEvent.click(screen.getByRole("button", { name: "Edit Filters" }))
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
  fireEvent.click(screen.getByRole("button", { name: "Edit Filters" }))
  const dialog = screen.getByRole("dialog", { name: "Edit Filters" })
  fireEvent.click(within(dialog).getByRole("button", { name: /^Company/ }))
  const company = within(dialog).getByPlaceholderText("e.g., Google")
  fireEvent.change(company, { target: { value: "Stripe" } })
  fireEvent.keyDown(company, { key: "Enter" })
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }))
  expect(screen.queryByText("Companies:")).toBeNull()
  execute()
  expect(store.send).toHaveBeenCalledWith("execute")
})

it("selects a location with its returned type", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          suggestions: [
            { id: "Brooklyn", display_value: "Brooklyn", type: "city" },
            { id: "New York", display_value: "New York", type: "state" },
          ],
        }),
      ),
    ),
  )
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Add Location" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Search Location" }), { target: { value: "New" } })
  fireEvent.click(await screen.findByRole("button", { name: "Brooklyn city" }))
  fireEvent.click(screen.getByRole("button", { name: "New York state" }))
  execute()
  expect(sent().filters.cities).toEqual(["Brooklyn"])
  expect(sent().filters.states).toEqual(["New York"])
  vi.unstubAllGlobals()
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
  pick("Stage max", "series_b")
  execute()
  expect(sent().filters).toEqual({ funding_stage_max: "series_b" })
})

it("cycles the temporal from the trait chip and edits its value behind the pencil", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "VP of Finance: current" }))
  expect(screen.getByRole("button", { name: "VP of Finance: past" })).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Edit VP of Finance" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Trait 1" }), { target: { value: "CFO" } })
  fireEvent.click(screen.getByRole("button", { name: "Done editing" }))
  expect(screen.getByRole("button", { name: "CFO: past" })).toBeTruthy()
})

it("sends a temporal change with the filter edits beside it: an in-place edit, not a new search", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.click(screen.getByRole("button", { name: "VP of Finance: current" }))
  execute()
  expect(sent()).toEqual({
    filters: { seniority_bands: ["vice-president"] },
    traits: [
      { value: "VP of Finance", temporal: "past", meaning: "role" },
      { value: "investment banking", temporal: "past", meaning: "experience" },
    ],
  })
})

it("shows money in millions and sends dollars", () => {
  const search: PreparedSearch = {
    ...SEARCH,
    filters: [...SEARCH.filters, { key: "funding_amount_min", value: 2_500_000 }],
  }
  render(<SearchCard search={search} actions={true} />)
  expect(screen.getByRole<HTMLInputElement>("spinbutton", { name: "Funding ($M) min" }).value).toBe("2.5")
  fireEvent.change(screen.getByRole("spinbutton", { name: "Funding ($M) max" }), { target: { value: "40" } })
  execute()
  expect(sent().filters).toEqual({ funding_amount_max: 40_000_000 })
})

it("drops a row emptied before Execute once the card is read-only, and offers no investors or set interactions locally", () => {
  const { rerender } = render(<SearchCard search={{ ...SEARCH, local: true }} actions={true} />)
  fireEvent.change(screen.getByRole("spinbutton", { name: "Years min" }), { target: { value: "" } })
  // Still there while editing, so it can be refilled.
  expect(screen.getByRole("spinbutton", { name: "Years min" })).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Edit Filters" }))
  const dialog = screen.getByRole("dialog", { name: "Edit Filters" })
  fireEvent.click(within(dialog).getByRole("button", { name: /^Interactions/ }))
  expect(within(dialog).getByText("Individual (You)")).toBeTruthy()
  expect(within(dialog).queryByText("Collective (Set)")).toBeNull()
  fireEvent.click(within(dialog).getByRole("button", { name: /^Company/ }))
  expect(within(dialog).queryByText("Investors")).toBeNull()
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }))
  execute()
  expect(sent().filters).toEqual({ years_experience_min: null })
  rerender(<SearchCard search={{ ...SEARCH, local: true }} actions={false} />)
  expect(screen.queryByText("Years:")).toBeNull()
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
  fireEvent.click(screen.getByRole("button", { name: "Add trait" }))
  fireEvent.change(screen.getByRole("textbox", { name: "Add trait" }), { target: { value: "CFO" } })
  execute()
  rerender(<SearchCard search={SEARCH} actions={false} />)
  expect(screen.getByRole("heading", { name: "“CFOs in NYC”" })).toBeTruthy()
  expect(screen.queryByText("Director")).toBeNull()
  expect(screen.getByText("CFO").parentElement?.textContent).toContain("Current")
  expect(screen.getAllByRole("button")).toHaveLength(1)
})

it("keeps the role ids it does not show", () => {
  render(<SearchCard search={SEARCH} actions={true} />)
  expect(screen.queryByText("Roles:")).toBeNull()
  expect(screen.queryByRole("button", { name: "Add Role ids" })).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  execute()
  // An edit beside them leaves the ids as prepared, so they are not sent as changed.
  expect(sent().filters).toEqual({ seniority_bands: ["vice-president"] })
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

it("hides what the cloud app's preview hides, and keeps sending it", () => {
  const search: PreparedSearch = {
    ...SEARCH,
    traits: [],
    filters: [
      ...SEARCH.filters,
      { key: "is_current_role", value: true },
      { key: "tech_skills", value: ["python"] },
      { key: "customer_types", value: ["b2b"] },
      { key: "graduation_year_min", value: 2010 },
    ],
  }
  render(<SearchCard search={search} actions={true} />)
  expect(screen.queryByRole("checkbox", { name: "Current role only" })).toBeNull()
  expect(screen.queryByText("Python")).toBeNull()
  expect(screen.queryByText("B2b")).toBeNull()
  expect(screen.queryByText("Graduation Year:")).toBeNull()
  execute()
  expect(store.send).toHaveBeenCalledWith("execute")
})

it("folds a list past ten names behind + N hidden, as the cloud app does", () => {
  const cities = Array.from({ length: 13 }, (_, at) => `City ${at + 1}`)
  render(
    <SearchCard
      search={{
        ...SEARCH,
        filters: [
          { key: "cities", value: cities },
          { key: "company_names", value: cities },
        ],
      }}
      actions={false}
    />,
  )
  fireEvent.click(screen.getByRole("button", { name: "Show filters" }))
  // Companies fold; locations are their own rows and show every name.
  fireEvent.click(screen.getByRole("button", { name: "+ 3 hidden" }))
  expect(screen.getByRole("button", { name: "Hide" })).toBeTruthy()
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
  expect(screen.getByText("Director")).toBeTruthy()
  expect(screen.getByRole("button", { name: "VP of Finance: current" })).toBeTruthy()
  execute()
  expect(store.send).toHaveBeenCalledWith("execute")
})

it("sends a blocked card's changes without running it", () => {
  render(<SearchCard search={{ ...SEARCH, blockers: ["bm25_queries is empty"] }} actions={true} />)
  expect(screen.queryByRole("button", { name: "Run Search" })).toBeNull()
  const changes = screen.getByRole<HTMLButtonElement>("button", { name: "Send changes" })
  expect(changes.disabled).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  fireEvent.click(changes)
  expect(message().startsWith("Modify:\n")).toBe(true)
  expect(message()).not.toContain("then execute")
})

it("saves the edits beside a chat prepare and says only that in the chat", async () => {
  const calls: { url: string; body: unknown }[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: { body: string }) => {
      calls.push({ url, body: JSON.parse(init.body) })
      return Promise.resolve(new Response("{}"))
    }),
  )
  render(<SearchCard search={{ ...SEARCH, run: "ab12-vp-finance" }} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  execute()
  await waitFor(() => expect(store.send).toHaveBeenCalledWith("Run the search with my edits"))
  expect(calls).toEqual([
    {
      url: "/searches/api/card-edits",
      body: { run: "ab12-vp-finance", edits: { filters: { seniority_bands: ["vice-president"] } } },
    },
  ])
  vi.unstubAllGlobals()
})

it("puts the edits in the message when the save fails", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("{}", { status: 400 }))),
  )
  render(<SearchCard search={{ ...SEARCH, run: "ab12-vp-finance" }} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  execute()
  await waitFor(() => expect(store.send).toHaveBeenCalled())
  expect(sent().filters).toEqual({ seniority_bands: ["vice-president"] })
  vi.unstubAllGlobals()
})

const RUN =
  "uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py run --ledger x"
const DIRECT = {
  ...SEARCH,
  run: "ab12-vp-finance",
  runDir: "/Users/casey/powerpacks/.powerpacks/search/ab12-vp-finance",
  executeCommand: RUN,
}

it("runs the prepared search on the chat itself, with no message to the agent", () => {
  render(<SearchCard search={DIRECT} actions={true} />)
  execute()
  expect(store.runCommand).toHaveBeenCalledWith(RUN)
  expect(store.send).not.toHaveBeenCalled()
})

it("runs filter edits directly with the saved edits file", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("{}"))),
  )
  render(<SearchCard search={DIRECT} actions={true} />)
  fireEvent.click(screen.getByRole("button", { name: "Remove director" }))
  execute()
  await waitFor(() =>
    expect(store.runCommand).toHaveBeenCalledWith(
      `${RUN} --edits-json '/Users/casey/powerpacks/.powerpacks/search/ab12-vp-finance/card-edits.json'`,
    ),
  )
  expect(store.send).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
})

it("leaves a changed query to the agent, which prepares it again", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response("{}"))),
  )
  render(<SearchCard search={DIRECT} actions={true} />)
  fireEvent.change(screen.getByRole("textbox", { name: "Query" }), { target: { value: "CFOs" } })
  execute()
  await waitFor(() => expect(store.send).toHaveBeenCalledWith("Run the search with my edits"))
  expect(store.runCommand).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
})

it("opens the full role query in a multiline editor and keeps edits", () => {
  render(
    <SearchCard
      search={{
        ...SEARCH,
        filters: [...SEARCH.filters, { key: "semantic_query", value: "Long role description" }],
      }}
      actions={true}
    />,
  )
  fireEvent.click(screen.getByRole("button", { name: "Edit role query" }))
  const editor = screen.getByRole("textbox", { name: "Role query" })
  expect(editor.tagName).toBe("TEXTAREA")
  fireEvent.change(editor, { target: { value: "Updated role description" } })
  fireEvent.click(screen.getByRole("button", { name: "Done" }))
  execute()
  expect(sent().filters.semantic_query).toBe("Updated role description")
})
