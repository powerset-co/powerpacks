import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { EMPTY_AGENT } from "@/lib/agent/store"
import { CARDS, RUN_ID } from "@/testing/searches-fixture"
import type { SearchCard } from "@/types/searches"

import { AgentPage } from "./AgentPage"

// The page around the results pane: a signed-in desktop chat whose agent is running a search.
vi.mock("@/lib/desktop", () => ({ isDesktop: () => true, invoke: vi.fn() }))
vi.mock("@/lib/features", () => ({ useFeatures: () => ({ sets: false, auto_reply: false }) }))
vi.mock("@/lib/agent/useCodexAccount", () => ({
  useCodexAccount: () => ({ status: { installed: true, account: { email: "me@example.com" } } }),
}))
vi.mock("@/lib/agent/useSearchScope", () => ({ useSearchScope: vi.fn() }))
vi.mock("@/lib/agent/useThreads", () => ({ useThreads: () => ({ data: [] }) }))
vi.mock("@/lib/agent/store", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  useAgent: () => ({ ...EMPTY_AGENT, running: true, searchRun: openRun }),
}))
vi.mock("./collaboration/useCollaboration", () => ({ useCollaboration: () => ({ data: undefined }) }))
vi.mock("./Composer", () => ({ Composer: () => null }))
vi.mock("@/pages/searches/RunColumn", () => ({
  RunColumn: ({ runId }: { runId: string }) => <div data-testid="results">{runId}</div>,
}))

// A finished search in the same catalog.
const FINISHED = "morgan-role"

// The run the chat has open, and RUN_ID's status in the catalog.
let openRun = RUN_ID
let status = "running"

beforeEach(() => {
  openRun = RUN_ID
  status = "running"
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      const searches: SearchCard[] = CARDS.map((card) =>
        card.run_id === RUN_ID ? { ...card, status } : card,
      )
      return Promise.resolve(new Response(JSON.stringify({ searches })))
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(client, "invalidateQueries")
  const page = () => (
    <QueryClientProvider client={client}>
      <AgentPage />
    </QueryClientProvider>
  )
  const { rerender } = render(page())
  const runReads = () => invalidate.mock.calls.filter(([filters]) => filters?.queryKey?.[1] === "run")
  return { client, runReads, rerender: () => rerender(page()) }
}

it("reads the run's ranked people again when it stops running", async () => {
  const { client, runReads } = renderPage()
  // Listed as running: the results pane opens on its screened people.
  expect((await screen.findByTestId("results")).textContent).toBe(RUN_ID)
  expect(runReads()).toEqual([])

  status = "completed"
  await act(() => client.refetchQueries({ queryKey: ["searches", "catalog"] }))
  await waitFor(() => expect(runReads()).toEqual([[{ queryKey: ["searches", "run", RUN_ID] }]]))
})

it("does not read a finished run again when the chat switches to it from a running one", async () => {
  const { runReads, rerender } = renderPage()
  expect((await screen.findByTestId("results")).textContent).toBe(RUN_ID)

  openRun = FINISHED
  rerender()
  expect(screen.getByTestId("results").textContent).toBe(FINISHED)
  expect(runReads()).toEqual([])
})
