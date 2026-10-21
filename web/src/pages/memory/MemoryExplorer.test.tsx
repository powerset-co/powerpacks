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
it("search selection highlights its graph and clicking a person focuses their connected nodes", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: false }))
  const fetch = vi.fn((input: string) => {
    const url = new URL(input, "http://localhost")
    let payload: unknown = GRAPH
    if (url.pathname.endsWith("/search"))
      payload = [
        {
          selector: { origin: "topic", value: "family" },
          count: 1,
          evidence: [],
          match: "saved lexical match",
        },
      ]
    if (url.pathname.endsWith("/people"))
      payload = {
        corpus: "synthetic",
        people: [{ parent: "parent-1", name: "Jordan Example", members: [], evidence: [] }],
        total: 1,
        next_after: "",
      }
    if (url.pathname.endsWith("/graph") && url.searchParams.has("origin"))
      payload = { ...GRAPH, selected_ids: ["parent-1", "topic:family"] }
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
  fireEvent.click(await screen.findByRole("button", { name: "family topic · 1" }))
  await waitFor(() => expect(graphCalls.select).toHaveBeenCalledWith("topic:family"))
  await waitFor(() => expect(graphCalls.highlight).toHaveBeenCalledWith(["parent-1", "topic:family"]))
  const person = (await screen.findAllByRole("button", { name: "Jordan Example" }))[0]
  if (!person) throw new Error("Person result missing")
  fireEvent.click(person)
  await waitFor(() => expect(graphCalls.select).toHaveBeenCalledWith("parent-1"))
  expect(fetch.mock.calls.some(([url]) => url.includes("parent=parent-1"))).toBe(true)
  fireEvent.click(screen.getByRole("button", { name: "Show all" }))
  await waitFor(() => expect(graphCalls.select).toHaveBeenLastCalledWith(null))
  cleanup()
  expect(graphCalls.destroy).toHaveBeenCalled()
})
