import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { createSet, deleteSet, fetchSearchScope, fetchSetDetail, inviteToSet } from "@/lib/api/searches"
import { open, options } from "@/testing/select"
import { SetsPage } from "./SetsPage"

vi.mock("@/lib/api/searches", () => ({
  fetchSearchScope: vi.fn(),
  fetchSetDetail: vi.fn(),
  inviteToSet: vi.fn(),
  createSet: vi.fn(),
  deleteSet: vi.fn(),
}))
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
  open("Search in")
  expect(options()).toEqual(["Team · 40,795", "Local index"])
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
      {
        user_id: "auth0|a",
        role: "owner",
        name: "Ada",
        email: "ada@x.co",
        person_count: 300,
        joined_at: "2026-11-11T00:00:00Z",
      },
      {
        user_id: "auth0|b",
        role: "member",
        name: null,
        email: "bo@x.co",
        person_count: 1,
        joined_at: "2026-11-11T00:00:00Z",
      },
    ],
  })
  setup("/sets?set=t-1")
  expect(await screen.findByText("Ada")).toBeTruthy()
  expect(fetchSetDetail).toHaveBeenCalledWith("t-1")
  expect(screen.getByText("40,795 unique contacts")).toBeTruthy()
  expect(screen.getByText("2 members")).toBeTruthy()
  expect(screen.getByText("bo@x.co")).toBeTruthy()
  expect(screen.getByText("300")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Invite" })).toBeNull()
})

it("gives the owner an invite link to copy", async () => {
  vi.mocked(fetchSetDetail).mockResolvedValue({
    id: "t-1",
    name: "Team",
    role: "owner",
    member_count: 1,
    person_count: 300,
    members: [
      {
        user_id: "auth0|a",
        role: "owner",
        name: "Ada",
        email: "ada@x.co",
        person_count: 300,
        joined_at: "2026-11-11T00:00:00Z",
      },
    ],
  })
  vi.mocked(inviteToSet).mockResolvedValue({ invite_url: "https://app.powerset.co/sets/invite/tok" })
  setup("/sets?set=t-1")
  fireEvent.click(await screen.findByRole("button", { name: "Invite" }))
  expect(await screen.findByText("https://app.powerset.co/sets/invite/tok")).toBeTruthy()
  expect(inviteToSet).toHaveBeenCalledWith("t-1")
})

it("makes a new set from its name and asks again for your sets", async () => {
  vi.mocked(createSet).mockResolvedValue({ id: "n-1" })
  setup("/sets")
  fireEvent.click(await screen.findByRole("button", { name: "New Set" }))
  fireEvent.change(screen.getByLabelText("Set name"), { target: { value: "Friends" } })
  fireEvent.click(screen.getByRole("button", { name: "Create" }))
  await vi.waitFor(() => expect(fetchSearchScope).toHaveBeenCalledTimes(2))
  expect(createSet).toHaveBeenCalledWith("Friends")
})

it("deletes a set you own after you confirm", async () => {
  vi.mocked(fetchSetDetail).mockResolvedValue({
    id: "t-1",
    name: "Team",
    role: "owner",
    member_count: 1,
    person_count: 300,
    members: [],
  })
  vi.mocked(deleteSet).mockResolvedValue()
  setup("/sets?set=t-1")
  fireEvent.click(await screen.findByRole("button", { name: "Delete set" }))
  expect(deleteSet).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole("button", { name: "Delete set" }))
  await vi.waitFor(() => expect(deleteSet).toHaveBeenCalledWith("t-1"))
})
