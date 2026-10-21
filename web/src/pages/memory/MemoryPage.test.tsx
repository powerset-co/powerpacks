import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import type { MemoryJob, MemoryPeoplePage } from "@/types/memory"

import { MemoryPage } from "./MemoryPage"

const IDLE: MemoryJob = {
  enabled: false,
  keep_updated: true,
  step: "idle",
  status: "idle",
  steps: {
    import: { done: 0, total: 0 },
    generate: { done: 0, total: 0 },
    index: { done: 0, total: 0 },
    discover: { done: 0, total: 0 },
  },
  current_item: "",
  people_indexed: 0,
  started_at: "",
  updated_at: "",
  approved_spend_usd: 0,
  spent_usd: 0,
  tokens: { input: 0, output: 0, embedding: 0 },
  failures: [],
  error: "",
  stopped: "",
}
const FIRST_RUN: MemoryJob = {
  ...IDLE,
  inventory: { counts: { parents: 1234, facts: 5678 } },
  sources: ["saved facts", "human tags and notes", "cached profiles", "display names"],
  estimate: {
    pending_chunks: 40,
    generation_input_tokens: 90_000,
    generation_output_token_cap: 160_000,
    embedding_input_tokens: 2_000,
    estimated_cost_usd: 2.341,
  },
}
const RUNNING: MemoryJob = {
  ...IDLE,
  enabled: true,
  status: "running",
  step: "generate",
  approved_spend_usd: 2.35,
  spent_usd: 0.4,
  started_at: "2026-10-20T10:00:00Z",
  current_item: "synthetic-source-17",
  steps: { ...IDLE.steps, import: { done: 12, total: 12 }, generate: { done: 7, total: 40 } },
}
const ENABLED: MemoryJob = {
  ...RUNNING,
  status: "done",
  step: "done",
  current_item: "",
  people_indexed: 980,
  updated_at: new Date().toISOString(),
  spent_usd: null,
  tokens: { input: 1000, output: 200, embedding: 30 },
  steps: {
    import: { done: 12, total: 12 },
    generate: { done: 39, total: 40 },
    index: { done: 300, total: 300 },
    discover: { done: 4, total: 4 },
  },
  failures: [
    {
      step: "generate",
      key: "synthetic-source-3:2",
      error: "provider timed out",
      at: "2026-10-20T10:05:00Z",
    },
  ],
}

type Reply = (url: string, init?: RequestInit) => unknown

function serve(record: MemoryJob, routes: Record<string, Reply> = {}) {
  const fetch = vi.fn((input: string, init?: RequestInit) => {
    const path = input.split("?")[0] ?? input
    const reply = routes[`${init?.method ?? "GET"} ${path}`]
    const payload = reply
      ? reply(input, init)
      : path === "/api/memory/graph"
        ? { nodes: [], edges: [], selected_ids: [], total_people: 0, total_topics: 0, truncated: false }
        : record
    return Promise.resolve(new Response(JSON.stringify(payload)))
  })
  vi.stubGlobal("fetch", fetch)
  vi.stubGlobal("matchMedia", () => ({ matches: false }))
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryPage />
    </QueryClientProvider>,
  )
  return fetch
}

// jsdom has no layout; the virtualized people list reads its viewport's and rows' offset sizes.
function stubLayout() {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 360 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 800 })
}

function posted(fetch: ReturnType<typeof serve>, path: string): unknown {
  const call = fetch.mock.calls.find(([url, init]) => url === path && init?.method === "POST")
  if (typeof call?.[1]?.body !== "string") throw new Error(`no POST to ${path}`)
  return JSON.parse(call[1].body)
}

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  vi.unstubAllGlobals()
})

it("before enabling shows the people, sources and estimate, and enables with the spend limit", async () => {
  let record = FIRST_RUN
  const fetch = serve(FIRST_RUN, {
    "GET /api/memory": () => record,
    "POST /api/memory/enable": () => {
      record = RUNNING
      return RUNNING
    },
  })
  expect(await screen.findByText("1,234")).toBeTruthy()
  expect(screen.getByText(/saved facts, human tags and notes/)).toBeTruthy()
  expect(screen.getByText("about $2.34")).toBeTruthy()
  const limit = screen.getByLabelText<HTMLInputElement>("Spend limit (USD)")
  expect(limit.value).toBe("2.35")
  fireEvent.change(limit, { target: { value: "5" } })
  fireEvent.click(screen.getByRole("button", { name: "Enable Memory" }))
  await waitFor(() => expect(posted(fetch, "/api/memory/enable")).toEqual({ approved_spend_usd: 5 }))
  expect(await screen.findByText(/Closing the page keeps it running/)).toBeTruthy()
})

it("asks for Deep Context first when there is nothing to remember", async () => {
  serve({ ...IDLE, inventory: { counts: {} }, sources: [], estimate: null })
  expect(await screen.findByText(/process your contacts with Deep Context first/)).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Enable Memory" })).toBeNull()
})

it("shows why an enable could not start", async () => {
  serve({ ...FIRST_RUN, status: "failed", error: "OPENAI_API_KEY is not set" })
  expect((await screen.findByRole("alert")).textContent).toBe("OPENAI_API_KEY is not set")
})

it("keeps the estimate and Enable Memory after an enable that could not start", async () => {
  const failed = { ...IDLE, status: "failed" as const, error: "OPENAI_API_KEY is not set" }
  let record = FIRST_RUN
  serve(FIRST_RUN, {
    "GET /api/memory": () => record,
    // The server's answer to the enable carries no estimate; its GET does.
    "POST /api/memory/enable": () => {
      record = { ...FIRST_RUN, ...failed }
      return failed
    },
  })
  fireEvent.click(await screen.findByRole("button", { name: "Enable Memory" }))
  expect((await screen.findByRole("alert")).textContent).toBe("OPENAI_API_KEY is not set")
  expect(screen.getByText("about $2.34")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Enable Memory" })).toBeTruthy()
})

it("while running lists the four steps with counts and the bar without internal checkpoint keys", async () => {
  serve(RUNNING)
  const steps = await screen.findByRole("list", { name: "Memory steps" })
  const rows = within(steps).getAllByRole("listitem")
  expect(rows.map((row) => row.textContent)).toEqual([
    expect.stringContaining("12 / 12 sources"),
    expect.stringContaining("7 / 40 chunks"),
    expect.stringContaining("Next"),
    expect.stringContaining("Next"),
  ])
  expect(rows[1]?.getAttribute("aria-current")).toBe("step")
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("7")
  expect(screen.queryByText("synthetic-source-17")).toBeNull()
  const running = screen.getByRole("region", { name: "Building your memory" })
  expect(within(running).getByText(/\$0\.40 of \$2\.35 approved/)).toBeTruthy()
})

it("shows completed discovery batches when its total is unknown", async () => {
  serve({ ...RUNNING, step: "discover", steps: { ...RUNNING.steps, discover: { done: 7, total: 0 } } })
  expect(await screen.findByText("7 batches completed")).toBeTruthy()
  expect(screen.getByRole("progressbar").hasAttribute("aria-valuenow")).toBe(false)
  fireEvent.click(screen.getByRole("button", { name: "Details" }))
  expect(await screen.findByText("7 batches")).toBeTruthy()
  expect(screen.queryByText("7 / 0 batches")).toBeNull()
})

it("shows status above the graph and offers updates only when inputs changed", async () => {
  const fetch = serve({ ...ENABLED, update_available: true }, { "POST /api/memory/update": () => RUNNING })
  const status = await screen.findByRole("heading", { name: "Ready" })
  const viewer = screen.getByRole("region", { name: "Explore memory" })
  expect(status.compareDocumentPosition(viewer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.queryByRole("checkbox", { name: "Keep memory updated" })).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "Update now" }))
  await waitFor(() => expect(posted(fetch, "/api/memory/update")).toEqual({}))
})

it("disables Updated when there are no new inputs and keeps Details collapsed", async () => {
  serve({ ...ENABLED, update_available: false })
  const button = await screen.findByRole<HTMLButtonElement>("button", { name: "Updated" })
  expect(button.disabled).toBe(true)
  expect(screen.getByRole("button", { name: "Details" }).getAttribute("aria-expanded")).toBe("false")
})

it("Details shows coverage, failures to retry and the cost in tokens when prices are unknown", async () => {
  serve(ENABLED)
  fireEvent.click(await screen.findByRole("button", { name: "Details" }))
  expect(screen.getByText("39 / 40 chunks")).toBeTruthy()
  expect(screen.getByText(/Update now retries these first/)).toBeTruthy()
  expect(screen.getByText("provider timed out")).toBeTruthy()
  expect(screen.getByText(/1,230 tokens \(prices unknown\)/)).toBeTruthy()
})

it("search resolves meanings, lists the first one's people and loads more", async () => {
  stubLayout()
  const page = (after: string): MemoryPeoplePage => ({
    corpus: "v2",
    selector: { origin: "human_tag", value: "investor" },
    total: 2,
    people: [
      after
        ? { parent: "p2", name: "Synthetic Two", members: ["m2"], evidence: [] }
        : { parent: "p1", name: "Synthetic One", members: ["m1"], evidence: [] },
    ],
    next_after: after ? "" : "p1",
  })
  const fetch = serve(ENABLED, {
    "GET /api/memory/search": () => [
      {
        selector: { origin: "human_tag", value: "investor" },
        count: 2,
        evidence: [],
        match: "exact or confirmed alias",
      },
    ],
    "GET /api/memory/people": (url) => page(new URL(url, "http://local").searchParams.get("after") ?? ""),
  })
  fireEvent.change(await screen.findByRole("searchbox", { name: "Search memory" }), {
    target: { value: "investor" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Search" }))
  expect(await screen.findByRole("button", { name: /investor your tag · 2/ })).toBeTruthy()
  expect(await screen.findByText("Synthetic One")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Load more" }))
  expect(await screen.findByText("Synthetic Two")).toBeTruthy()
  expect(screen.getByText("2 of 2 people")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull()
  expect(fetch.mock.calls.map(([url]) => url)).toContain(
    "/api/memory/people?origin=human_tag&value=investor&after=p1&limit=50",
  )
})

it("lists a suggestion's people only once it is picked", async () => {
  stubLayout()
  const fetch = serve(ENABLED, {
    "GET /api/memory/search": () => [
      {
        selector: { origin: "topic", value: "climate" },
        count: 1,
        evidence: [],
        match: "suggestion; confirm meaning before enumeration",
      },
    ],
    "GET /api/memory/people": () => ({
      corpus: "v2",
      selector: { origin: "topic", value: "climate" },
      total: 1,
      people: [{ parent: "p1", name: "Synthetic One", members: ["m1"], evidence: [] }],
      next_after: "",
    }),
  })
  fireEvent.change(await screen.findByRole("searchbox", { name: "Search memory" }), {
    target: { value: "carbon" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Search" }))
  const meaning = await screen.findByRole("button", { name: /climate topic · 1/ })
  expect(meaning.getAttribute("aria-pressed")).toBe("false")
  expect(fetch.mock.calls.some(([url]) => url.startsWith("/api/memory/people"))).toBe(false)
  fireEvent.click(meaning)
  expect(await screen.findByText("Synthetic One")).toBeTruthy()
})

it("polls every 2 s while a run works and stops once it is done", async () => {
  vi.useFakeTimers()
  let record = RUNNING
  const fetch = vi.fn((url: string) =>
    Promise.resolve(
      new Response(
        JSON.stringify(
          url === "/api/memory"
            ? record
            : { nodes: [], edges: [], selected_ids: [], total_people: 0, total_topics: 0, truncated: false },
        ),
      ),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  vi.stubGlobal("matchMedia", () => ({ matches: false }))
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryPage />
    </QueryClientProvider>,
  )
  await act(() => vi.advanceTimersByTimeAsync(100))
  expect(fetch.mock.calls.filter(([url]) => url === "/api/memory")).toHaveLength(1)
  record = ENABLED
  await act(() => vi.advanceTimersByTimeAsync(2_000))
  expect(fetch.mock.calls.filter(([url]) => url === "/api/memory")).toHaveLength(2)
  expect(screen.getByRole("heading", { name: "Ready" })).toBeTruthy()
  await act(() => vi.advanceTimersByTimeAsync(6_000))
  expect(fetch.mock.calls.filter(([url]) => url === "/api/memory")).toHaveLength(2)
})
