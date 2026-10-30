import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { EMPTY_AGENT, setScope, useAgent, type AgentState } from "@/lib/agent/store"
import { fetchSearchScope } from "@/lib/api/searches"
import { open, options, picked } from "@/testing/select"
import type { SearchScope as Scope } from "@/types/searches"
import { SearchScope } from "./SearchScope"

vi.mock("@/lib/api/searches", () => ({ fetchSearchScope: vi.fn() }))
vi.mock("@/lib/agent/store", async (actual: () => Promise<Record<string, unknown>>) => ({
  ...(await actual()),
  useAgent: vi.fn(),
  setScope: vi.fn(),
}))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

// Largest first, as the server sends them.
const SCOPE: Scope = {
  sets: [
    { set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40795 },
    { set_id: "p-1", name: "Personal Connections", is_personal: true, owned: true, person_count: 15 },
  ],
  local_db: "/idx/local-search.duckdb",
}

function setup(agent: Partial<AgentState> = {}) {
  vi.mocked(useAgent).mockReturnValue({ ...EMPTY_AGENT, ...agent })
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SearchScope />
    </QueryClientProvider>,
  )
}

const SEARCH_IN = "Search in"

it("lists the Powerset sets with their people and this Mac's local index, and a pick is the next chat's", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  setup({ scope: { set_id: "t-1" } })
  await screen.findByRole("combobox", { name: SEARCH_IN })
  // The pill shows only the pick, so it is as wide as the pick rather than the longest set.
  expect(picked(SEARCH_IN)).toBe("Team · 40,795")
  open(SEARCH_IN)
  expect(options()).toEqual(["Team · 40,795", "Personal network · 15", "Local index"])
  expect(screen.getByText("Powerset")).toBeTruthy()
  expect(screen.getByText("This Mac")).toBeTruthy()
  fireEvent.click(screen.getByRole("option", { name: "Local index" }))
  expect(setScope).toHaveBeenCalledWith({ local_db: "/idx/local-search.duckdb" })
})

it("starts a new chat on the local index when nothing was picked, else on the largest set", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  setup()
  await screen.findByRole("combobox", { name: SEARCH_IN })
  expect(picked(SEARCH_IN)).toBe("Local index")
  await waitFor(() => expect(setScope).toHaveBeenCalledWith({ local_db: "/idx/local-search.duckdb" }))
  cleanup()
  vi.mocked(setScope).mockClear()
  vi.mocked(fetchSearchScope).mockResolvedValue({ ...SCOPE, local_db: null })
  setup()
  await screen.findByRole("combobox", { name: SEARCH_IN })
  expect(picked(SEARCH_IN)).toBe("Team · 40,795")
  await waitFor(() => expect(setScope).toHaveBeenCalledWith({ set_id: "t-1" }))
  open(SEARCH_IN)
  expect(options()).toEqual(["Team · 40,795", "Personal network · 15"])
  expect(screen.queryByText("This Mac")).toBeNull()
})

it("pins the scope once the chat has started", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  setup({ threadId: "t1", scope: { set_id: "p-1" } })
  const pill = await screen.findByRole("combobox", { name: SEARCH_IN })
  expect(picked(SEARCH_IN)).toBe("Personal network · 15")
  expect(pill).toHaveProperty("disabled", true)
  expect(setScope).not.toHaveBeenCalled()
})
