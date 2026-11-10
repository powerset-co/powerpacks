import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { fetchSearchScope } from "@/lib/api/searches"
import { open, options, picked } from "@/testing/select"
import type { SearchScope } from "@/types/searches"
import { ScopePicker } from "./ScopePicker"

vi.mock("@/lib/api/searches", () => ({ fetchSearchScope: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const SCOPE: SearchScope = {
  sets: [
    { set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40795 },
    { set_id: "p-1", name: "Personal Connections", is_personal: true, owned: true, person_count: 15 },
  ],
  local_db: "/idx/local-search.duckdb",
}

function setup(onChange = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ScopePicker value={{ set_id: "t-1" }} onChange={onChange} />
    </QueryClientProvider>,
  )
  return onChange
}

it("hides your own personal set when this Mac has a local index, and hands back the pick", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  const onChange = setup()
  await screen.findByRole("combobox", { name: "Search in" })
  expect(picked("Search in")).toBe("Team · 40,795")
  open("Search in")
  expect(options()).toEqual(["Team · 40,795", "Local index"])
  expect(screen.getByText("Powerset")).toBeTruthy()
  expect(screen.getByText("This Mac")).toBeTruthy()
  fireEvent.click(screen.getByRole("option", { name: "Local index" }))
  expect(onChange).toHaveBeenCalledWith({ local_db: "/idx/local-search.duckdb" })
})

it("offers your personal set when there is no local index", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ ...SCOPE, local_db: null })
  setup()
  await screen.findByRole("combobox", { name: "Search in" })
  open("Search in")
  expect(options()).toEqual(["Team · 40,795", "Personal network · 15"])
  expect(screen.queryByText("This Mac")).toBeNull()
})
