import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { fetchSearchScope, fetchSetDetail } from "@/lib/api/searches"
import { open, options } from "@/testing/select"
import { SetsPage } from "./SetsPage"

vi.mock("@/lib/api/searches", () => ({ fetchSearchScope: vi.fn(), fetchSetDetail: vi.fn() }))
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

const SCOPE = {
  sets: [
    { set_id: "t-1", name: "Team", is_personal: false, owned: false, person_count: 40795 },
    { set_id: "p-1", name: "Personal Connections", is_personal: true, owned: true, person_count: 15 },
  ],
  local_db: "/idx/local-search.duckdb",
}

function setup(path: string) {
  vi.mocked(fetchSearchScope).mockResolvedValue(SCOPE)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <SetsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it("opens on the local index and offers your sets without the cloud copy of your own network", async () => {
  setup("/sets")
  expect(await screen.findByText("This Mac")).toBeTruthy()
  open("Choose a set")
  expect(options()).toEqual(["Local index", "Team"])
  expect(fetchSetDetail).not.toHaveBeenCalled()
})

it("shows a picked set's people, members and their shares", async () => {
  vi.mocked(fetchSetDetail).mockResolvedValue({
    id: "t-1",
    name: "Team",
    role: "member",
    member_count: 2,
    person_count: 40795,
    members: [
      { user_id: "auth0|a", role: "owner", name: "Ada", email: "ada@x.co", person_count: 300 },
      { user_id: "auth0|b", role: "member", name: null, email: "bo@x.co", person_count: 1 },
    ],
  })
  setup("/sets?set=t-1")
  expect(await screen.findByText("Ada")).toBeTruthy()
  expect(fetchSetDetail).toHaveBeenCalledWith("t-1")
  expect(screen.getByText("40,795 people")).toBeTruthy()
  expect(screen.getByText("2 members")).toBeTruthy()
  expect(screen.getByText("You joined this set")).toBeTruthy()
  expect(screen.getByText("bo@x.co")).toBeTruthy()
  expect(screen.getByText("300 people")).toBeTruthy()
})
