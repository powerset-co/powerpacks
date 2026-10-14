import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { fetchSearchScope, saveSearchScope } from "@/lib/api/searches"
import type { SearchScope as Scope } from "@/types/searches"
import { SearchScope } from "./SearchScope"

vi.mock("@/lib/api/searches", () => ({
  LOCAL_SCOPE: "local",
  fetchSearchScope: vi.fn(),
  saveSearchScope: vi.fn(),
}))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const SCOPE: Scope = {
  sets: [
    { set_id: "p-1", name: "Personal Connections", is_personal: true, owned: true, member_count: 1 },
    { set_id: "t-1", name: "Team", is_personal: false, owned: false, member_count: 6 },
  ],
  local: true,
  selected: "t-1",
}

function setup() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SearchScope />
    </QueryClientProvider>,
  )
}

it("lists the Powerset sets and this Mac's local index, and saves the pick", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  vi.mocked(saveSearchScope).mockResolvedValue({ ...SCOPE, selected: "local" })
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  expect(select.value).toBe("t-1")
  expect([...select.options].map((option) => option.text)).toEqual([
    "Personal network",
    "Team",
    "Local index",
  ])
  expect([...select.querySelectorAll("optgroup")].map((group) => group.label)).toEqual([
    "Powerset",
    "This Mac",
  ])
  fireEvent.change(select, { target: { value: "local" } })
  await waitFor(() => expect(select.value).toBe("local"))
  expect(saveSearchScope).toHaveBeenCalledWith("local", expect.anything())
})

it("leaves out the local index when this Mac has none", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ ...SCOPE, local: false })
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  expect([...select.options].map((option) => option.value)).toEqual(["p-1", "t-1"])
  expect(select.querySelectorAll("optgroup")).toHaveLength(1)
})

it("asks for a pick when .env names a set that isn't one of yours", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ ...SCOPE, selected: "" })
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  expect(select.value).toBe("")
  expect(select.selectedOptions[0]?.text).toBe("Pick a set")
})
