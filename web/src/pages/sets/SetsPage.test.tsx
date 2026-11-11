import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, expect, it, vi } from "vitest"

import { fetchSearchScope } from "@/lib/api/searches"
import { SetsPage } from "./SetsPage"

vi.mock("@/lib/api/searches", () => ({ fetchSearchScope: vi.fn() }))
// jsdom has no layout: the table's virtualizer reads the viewport's height to mount rows.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 800 })
})
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

it("lists the local index, then your sets without the cloud copy of your own network", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({
    sets: [
      { set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40795 },
      { set_id: "p-1", name: "Personal Connections", is_personal: true, owned: true, person_count: 15 },
    ],
    local_db: "/idx/local-search.duckdb",
  })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SetsPage />
    </QueryClientProvider>,
  )
  await screen.findByText("Local index")
  expect(screen.getByText("Team")).toBeTruthy()
  expect(screen.getByText("40,795")).toBeTruthy()
  expect(screen.queryByText("Personal network")).toBeNull()
  expect(screen.queryByText("Personal Connections")).toBeNull()
})
