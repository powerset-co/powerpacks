import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import type { AccountsPayload } from "@/types/accounts"

import { AccountsPage } from "./AccountsPage"

const api = vi.hoisted(() => ({ fetchAccounts: vi.fn(), reconnectGmail: vi.fn(), syncAccount: vi.fn() }))
vi.mock("@/lib/api/accounts", () => api)
vi.mock("@/lib/desktop", () => ({ isDesktop: () => true }))
vi.mock("./CodexCard", () => ({ CodexCard: () => <li data-account="codex">Codex</li> }))
afterEach(cleanup)

const PAYLOAD: AccountsPayload = {
  accounts: [
    {
      source: "gmail",
      name: "casey@example.com",
      health: "ok",
      note: "Up to date.",
      messages: 120,
      contacts: 40,
      latest_message_at: null,
      last_sync_at: null,
    },
  ],
  jobs: {},
  scheduled: true,
}

it("holds the cards' shapes under Codex while the accounts load, then shows them", async () => {
  let resolve: (payload: AccountsPayload) => void = () => undefined
  api.fetchAccounts.mockReturnValue(new Promise<AccountsPayload>((done) => (resolve = done)))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { container } = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )

  const list = screen.getByRole("list", { name: "Accounts" })
  expect(list.getAttribute("aria-busy")).toBe("true")
  expect(screen.getByText("Codex")).toBeTruthy()
  expect(screen.getByText("Loading accounts…")).toBeTruthy()
  expect(container.querySelectorAll("li[aria-hidden]")).toHaveLength(2)

  resolve(PAYLOAD)
  expect(await screen.findByText("casey@example.com")).toBeTruthy()
  expect(list.getAttribute("aria-busy")).toBe("false")
  expect(screen.getByText("Refreshes daily at 6:00 AM.")).toBeTruthy()
  expect(screen.queryByText("Loading accounts…")).toBeNull()
  expect(container.querySelectorAll("li[aria-hidden]")).toHaveLength(0)
})

it("shows a card's stats on a click on it and folds them on the next", async () => {
  api.fetchAccounts.mockResolvedValue(PAYLOAD)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )

  const name = await screen.findByText("casey@example.com")
  const card = name.closest("details")
  if (!card) throw new Error("Expected the card to fold")
  const stats = screen.getByText("Messages").closest("dl")
  expect(stats?.parentElement).toBe(card)
  expect(card.open).toBe(false)
  expect(card.querySelector("summary")?.textContent).toBe("casey@example.comHealthyUp to date.")

  fireEvent.click(name)
  expect(card.open).toBe(true)
  expect(screen.getByText("120")).toBeTruthy()
  fireEvent.click(name)
  expect(card.open).toBe(false)
})
