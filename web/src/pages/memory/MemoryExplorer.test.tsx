import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { MemoryExplorer } from "./MemoryExplorer"

const graphCalls = vi.hoisted(() => ({ select: vi.fn(), highlight: vi.fn(), destroy: vi.fn() }))
vi.mock("./graph", () => ({
  MemoryGraph: class {
    select = graphCalls.select
    highlight = graphCalls.highlight
    destroy = graphCalls.destroy
    motion = true
    depth = true
  },
}))
const GRAPH = {
  corpus: "synthetic",
  nodes: [
    { id: "parent-1", label: "Jordan Example", kind: "person" },
    { id: "topic:family", label: "family", kind: "topic" },
  ],
  edges: [{ source: "parent-1", target: "topic:family" }],
  selected_ids: [],
  total_people: 1,
  total_topics: 1,
  truncated: false,
}
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})
it("a search lists people and clicking one focuses their connected nodes", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false }))
  // The result list is virtualized: give rows a height so they render in jsdom.
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 360 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 800 })
  const fetch = vi.fn((input: string) => {
    const url = new URL(input, "http://localhost")
    let payload: unknown = GRAPH
    if (url.pathname.endsWith("/search"))
      payload = {
        people: [
          {
            parent: "parent-1",
            name: "Jordan Example",
            warmth: 2.4,
            score: 0.05,
            evidence: [
              {
                claim_id: 1,
                claim: { text: "family", kind: "tag", topic: "family", scope: "owner", object: "" },
                citations: [],
              },
            ],
            members: [],
          },
        ],
        layers: ["family"],
        filtered: false,
        total: 1,
        lexical_candidates: 0,
        vector_candidates: 0,
        missing_embeddings: 0,
      }
    if (url.pathname.endsWith("/graph") && url.searchParams.has("parent"))
      payload = { ...GRAPH, selected_ids: ["parent-1"] }
    return Promise.resolve(new Response(JSON.stringify(payload)))
  })
  vi.stubGlobal("fetch", fetch)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryExplorer running={false} />
    </QueryClientProvider>,
  )
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "family" } })
  fireEvent.click(screen.getByRole("button", { name: "Search" }))
  expect(await screen.findByText("1 person · everyone saved as family")).toBeTruthy()
  const person = (await screen.findAllByRole("button", { name: /Jordan Example/ }))[0]
  if (!person) throw new Error("Person result missing")
  fireEvent.click(person)
  await waitFor(() => expect(graphCalls.select).toHaveBeenCalledWith("parent-1"))
  expect(fetch.mock.calls.some(([url]) => url.includes("parent=parent-1"))).toBe(true)
  await waitFor(() => expect(graphCalls.highlight).toHaveBeenCalledWith(["parent-1"]))
  fireEvent.click(screen.getByRole("button", { name: "Show all" }))
  await waitFor(() => expect(graphCalls.select).toHaveBeenLastCalledWith(null))
  cleanup()
  expect(graphCalls.destroy).toHaveBeenCalled()
})
