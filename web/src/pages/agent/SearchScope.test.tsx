import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { fetchSearchScope, saveSearchScope } from "@/lib/api/searches"
import { open, options, pick, picked } from "@/testing/select"
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

const SEARCH_IN = "Search in"

it("lists the Powerset sets with their people and this Mac's local index, and shows a pick at once", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  let answer!: (scope: Scope) => void
  vi.mocked(saveSearchScope).mockReturnValue(new Promise<Scope>((resolve) => (answer = resolve)))
  setup()
  await screen.findByRole("combobox", { name: SEARCH_IN })
  // The pill shows only the pick, so it is as wide as the pick rather than the longest set.
  expect(picked(SEARCH_IN)).toBe("Team · 40,795")
  open(SEARCH_IN)
  expect(options()).toEqual(["Team · 40,795", "Personal network · 15", "Local index"])
  expect(screen.getByText("Powerset")).toBeTruthy()
  expect(screen.getByText("This Mac")).toBeTruthy()
  fireEvent.click(screen.getByRole("option", { name: "Local index" }))
  await waitFor(() => expect(picked(SEARCH_IN)).toBe("Local index"))
  expect(saveSearchScope).toHaveBeenCalledWith("local", expect.anything())
  answer({ ...SCOPE, selected: "local" })
  await waitFor(() => expect(picked(SEARCH_IN)).toBe("Local index"))
})

it("puts the old pick back when saving fails", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  vi.mocked(saveSearchScope).mockRejectedValue(new Error("Couldn't change where searches look."))
  setup()
  await screen.findByRole("combobox", { name: SEARCH_IN })
  pick(SEARCH_IN, "Personal network · 15")
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    "Couldn't change where searches look.",
  )
  expect(picked(SEARCH_IN)).toBe("Team · 40,795")
})

it("leaves out the local index when this Mac has none", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ ...SCOPE, local: false })
  setup()
  await screen.findByRole("combobox", { name: SEARCH_IN })
  open(SEARCH_IN)
  expect(options()).toEqual(["Team · 40,795", "Personal network · 15"])
  expect(screen.queryByText("This Mac")).toBeNull()
})

it("asks for a pick when signed out with a local index not picked", async () => {
  vi.mocked(fetchSearchScope).mockResolvedValue({ sets: [], local: true, selected: "" })
  setup()
  await screen.findByRole("combobox", { name: SEARCH_IN })
  expect(picked(SEARCH_IN)).toBe("Pick a set")
})
