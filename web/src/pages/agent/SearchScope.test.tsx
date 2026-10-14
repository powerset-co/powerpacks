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

// Largest first, as the server sends them.
const SCOPE: Scope = {
  sets: [
    { set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40795 },
    { set_id: "p-1", name: "Personal Connections", is_personal: true, owned: true, person_count: 15 },
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

it("lists the Powerset sets with their people and this Mac's local index, and shows a pick at once", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  let answer!: (scope: Scope) => void
  vi.mocked(saveSearchScope).mockReturnValue(new Promise<Scope>((resolve) => (answer = resolve)))
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  expect(select.value).toBe("t-1")
  expect([...select.options].map((option) => option.text)).toEqual([
    "Team · 40,795",
    "Personal network · 15",
    "Local index",
  ])
  expect([...select.querySelectorAll("optgroup")].map((group) => group.label)).toEqual([
    "Powerset",
    "This Mac",
  ])
  fireEvent.change(select, { target: { value: "local" } })
  await waitFor(() => expect(select.value).toBe("local"))
  expect(saveSearchScope).toHaveBeenCalledWith("local", expect.anything())
  answer({ ...SCOPE, selected: "local" })
  await waitFor(() => expect(select.value).toBe("local"))
})

it("puts the old pick back when saving fails", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  vi.mocked(saveSearchScope).mockRejectedValue(new Error("Couldn't change where searches look."))
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  fireEvent.change(select, { target: { value: "p-1" } })
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    "Couldn't change where searches look.",
  )
  expect(select.value).toBe("t-1")
})

it("leaves out the local index when this Mac has none", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ ...SCOPE, local: false })
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  expect([...select.options].map((option) => option.value)).toEqual(["t-1", "p-1"])
  expect(select.querySelectorAll("optgroup")).toHaveLength(1)
})

it("asks for a pick when signed out with a local index not picked", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ sets: [], local: true, selected: "" })
  setup()
  const select = await screen.findByRole<HTMLSelectElement>("combobox", { name: "Search in" })
  expect(select.value).toBe("")
  expect(select.selectedOptions[0]?.text).toBe("Pick a set")
})
