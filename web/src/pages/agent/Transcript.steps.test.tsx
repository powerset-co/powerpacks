import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { searchRunKey } from "@/pages/searches/hooks/useSearchRun"
import { goBackCommand } from "@/pages/searches/run/steps"
import { CARDS, QUICK_RUN, RUN_ID, STEPPED_RUN } from "@/testing/searches-fixture"
import type { Entry } from "@/types/agent"
import type { SearchRunPayload } from "@/types/searches"

import { Transcript } from "./Transcript"

const store = vi.hoisted(() => ({
  send: vi.fn(),
  runCommand: vi.fn(() => Promise.resolve(true)),
  useAgent: () => ({ scope: null }),
}))
vi.mock("@/lib/agent/store", () => store)
vi.mock("@/lib/agent/useSearchScope", () => ({ useSearchScope: () => ({ data: undefined }) }))

afterEach(() => {
  cleanup()
  store.send.mockReset()
  store.runCommand.mockReset()
  store.runCommand.mockImplementation(() => Promise.resolve(true))
})

const PIPELINE =
  "uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py"

function command(id: string, line: string, printed: Record<string, unknown>): Entry {
  return {
    kind: "command",
    id,
    command: line,
    status: "done",
    exitCode: 0,
    output: `${JSON.stringify({ primitive: "search_network_pipeline", ...printed }, null, 2)}\n`,
    durationMs: 900,
  }
}

function prepare(id: string, run: string): Entry {
  return command(id, `${PIPELINE} prepare --output-dir .powerpacks/search/${run}`, {
    query: "technical recruiters in Austin",
    status: "preview_ready",
    payload_json: `.powerpacks/search/${run}/payload.json`,
    preview: { filters: { metro_areas: ["Austin Metropolitan Area"] } },
  })
}
const PREPARE = prepare("c1", RUN_ID)
const RAN = command("c2", `${PIPELINE} execute`, { status: "completed", summary: { rows: 2 } })

function rerank(id: string, flag: string, step: Record<string, unknown>, run = RUN_ID): Entry {
  return command(id, `${PIPELINE} rerank --run-dir '.powerpacks/search/${run}' ${flag}`, {
    status: "completed",
    step: { run_id: run, cost_usd: 0.0064, ...step },
  })
}

const S2 = rerank("c3", "--criteria 'built training infra at scale'", {
  id: "s2",
  parent: "s1",
  criteria: "built training infra at scale",
  query_type: "rerank",
  shown: 2,
  scored: 2,
})
const S3 = rerank("c4", "--criteria 'only people still at a startup'", {
  id: "s3",
  parent: "s2",
  criteria: "still at a startup",
  query_type: "filter",
  shown: 2,
  scored: 3,
})
const BACK = rerank("c5", "--use 's1'", {
  id: "s1",
  parent: null,
  criteria: null,
  query_type: null,
  shown: 2,
})

function renderWith(
  entries: Entry[],
  run: SearchRunPayload,
  running = false,
  others: SearchRunPayload[] = [],
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(searchRunKey(RUN_ID), run)
  for (const other of others) client.setQueryData(searchRunKey(other.search.run_id), other)
  client.setQueryData(["searches", "catalog"], CARDS)
  render(
    <QueryClientProvider client={client}>
      <Transcript entries={entries} running={running} />
    </QueryClientProvider>,
  )
}

function cards(): HTMLElement[] {
  return screen.queryAllByRole("region", { name: "Refinement" })
}

it("shows each refinement as one line: verb, what changed, count; Showing now on the current one", () => {
  renderWith([PREPARE, RAN, S2, S3], STEPPED_RUN)
  const [reranked, narrowed] = cards()
  if (!reranked || !narrowed || cards().length !== 2) throw new Error("Expected two step cards")
  expect(reranked.textContent).toContain("Re-ranked")
  expect(reranked.textContent).toContain("built training infra at scale")
  expect(reranked.textContent).toContain("2 people")
  expect(within(reranked).getByRole("button", { name: "Go back to this" })).toBeTruthy()
  expect(narrowed.textContent).toContain("Narrowed")
  expect(narrowed.textContent).toContain("still at a startup")
  expect(narrowed.textContent).toContain("2 of 3 people")
  expect(narrowed.textContent).toContain("Showing now")
  expect(within(narrowed).queryByRole("button")).toBeNull()
  // The search card can go back to the search too; no step id is on screen.
  const search = screen.getByRole("region", { name: "Search" })
  expect(within(search).getByRole("button", { name: "Go back to this" })).toBeTruthy()
  expect(document.body.textContent).not.toMatch(/\bs[123]\b/)
})

it("goes back by a card's button: the --use command for that step, once", async () => {
  renderWith([PREPARE, RAN, S2, S3], STEPPED_RUN)
  const reranked = cards()[0]
  if (!reranked) throw new Error("Expected a step card")
  fireEvent.click(within(reranked).getByRole("button", { name: "Go back to this" }))
  fireEvent.click(
    within(screen.getByRole("region", { name: "Search" })).getByRole("button", { name: "Go back to this" }),
  )
  await Promise.resolve()
  expect(store.runCommand.mock.calls).toEqual([[goBackCommand(RUN_ID, "s2")], [goBackCommand(RUN_ID, "s1")]])
  expect(store.send).not.toHaveBeenCalled()
})

it("waits while a turn runs", () => {
  renderWith([PREPARE, RAN, S2, S3], STEPPED_RUN, true)
  const reranked = cards()[0]
  if (!reranked) throw new Error("Expected a step card")
  const button = within(reranked).getByRole("button", { name: "Go back to this" })
  expect(button.hasAttribute("disabled")).toBe(true)
})

it("shows a go-back as one line, with no card and no command", () => {
  const atSearch = { ...STEPPED_RUN, search: { ...STEPPED_RUN.search, current_step: "s1" } }
  renderWith([PREPARE, RAN, S2, S3, BACK], atSearch)
  expect(cards()).toHaveLength(2)
  expect(screen.getByText(`Went back to ${STEPPED_RUN.search.title}`)).toBeTruthy()
  expect(document.body.textContent).not.toContain("--use")
  const search = screen.getByRole("region", { name: "Search" })
  expect(search.textContent).toContain("Showing now")
  for (const card of cards())
    expect(within(card).getByRole("button", { name: "Go back to this" })).toBeTruthy()
})

it("gives each search card in the chat its own run's way back, and none to a run without steps", async () => {
  const OTHER = "riley-role"
  const other = { ...STEPPED_RUN, search: { ...STEPPED_RUN.search, run_id: OTHER, current_step: "s2" } }
  const unrefined = { ...QUICK_RUN, search: { ...QUICK_RUN.search, run_id: "morgan-role" } }
  const otherStep = rerank(
    "c8",
    "--criteria 'built training infra at scale'",
    {
      id: "s2",
      parent: "s1",
      criteria: "built training infra at scale",
      query_type: "rerank",
      shown: 2,
      scored: 2,
    },
    OTHER,
  )
  renderWith(
    [
      PREPARE,
      RAN,
      S2,
      S3,
      prepare("c6", OTHER),
      command("c7", `${PIPELINE} execute`, { status: "completed" }),
      otherStep,
      prepare("c9", "morgan-role"),
    ],
    STEPPED_RUN,
    false,
    [other, unrefined],
  )
  const [first, second, third] = screen.getAllByRole("region", { name: "Search" })
  if (!first || !second || !third) throw new Error("Expected three search cards")
  fireEvent.click(within(first).getByRole("button", { name: "Go back to this" }))
  fireEvent.click(within(second).getByRole("button", { name: "Go back to this" }))
  expect(within(third).queryByRole("button", { name: "Go back to this" })).toBeNull()
  expect(third.textContent).not.toContain("Showing now")
  await Promise.resolve()
  expect(store.runCommand.mock.calls).toEqual([[goBackCommand(RUN_ID, "s1")], [goBackCommand(OTHER, "s1")]])
})
