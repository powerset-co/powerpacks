import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"
import type { AccountsPayload } from "@/types/accounts"
import { AccountsSettings } from "./AccountsSettings"

const api = vi.hoisted(() => ({ fetchAccounts: vi.fn() }))
vi.mock("@/lib/api/accounts", () => api)
vi.mock("@/lib/desktop", () => ({ isDesktop: () => false }))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

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
  scheduled: "daily at 6:00 AM",
}
function mount() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <AccountsSettings />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
it("shows loading placeholders, then expandable account data", async () => {
  let resolve!: (payload: AccountsPayload) => void
  api.fetchAccounts.mockReturnValue(
    new Promise<AccountsPayload>((done) => {
      resolve = done
    }),
  )
  mount()
  expect(screen.getByLabelText("Loading accounts")).toBeTruthy()
  resolve(PAYLOAD)
  const title = await screen.findByText("Gmail")
  const details = title.closest("details")
  if (!details) throw new Error("Expected expandable account row")
  expect(details.open).toBe(false)
  fireEvent.click(title)
  expect(details.open).toBe(true)
  expect(screen.getByText("120")).toBeTruthy()
  expect(screen.getByText("Syncs daily at 6:00 AM.")).toBeTruthy()
  fireEvent.click(title)
  expect(details.open).toBe(false)
})
it("keeps each Gmail mailbox and sends an expired one to Scheduled tasks to reconnect", async () => {
  api.fetchAccounts.mockResolvedValue({
    ...PAYLOAD,
    accounts: [PAYLOAD.accounts[0], { ...PAYLOAD.accounts[0], name: "jordan@example.com", health: "error" }],
  })
  mount()
  fireEvent.click(await screen.findByText("Gmail"))
  expect(screen.getByText("casey@example.com")).toBeTruthy()
  expect(screen.getByText("jordan@example.com")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull()
  expect(screen.getByRole("link", { name: "Reconnect in Scheduled tasks" }).getAttribute("href")).toBe(
    "/tasks",
  )
})
it("never syncs from a card, and shows a running sync's step", async () => {
  const account = {
    ...PAYLOAD.accounts[0],
    source: "whatsapp" as const,
    name: "WhatsApp data",
    health: "warning" as const,
  }
  api.fetchAccounts.mockResolvedValue({
    ...PAYLOAD,
    accounts: [account],
    jobs: { whatsapp: { state: "running", step: "Syncing messages" } },
  })
  mount()
  fireEvent.click(await screen.findByText("WhatsApp"))
  expect(await screen.findByText("Syncing messages")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Sync" })).toBeNull()
})
