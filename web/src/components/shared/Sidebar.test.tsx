import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import { useSetupDone } from "@/hooks/useSetupDone"

import { Sidebar } from "./Sidebar"

const auth = vi.hoisted(() => ({ fetchAccount: vi.fn(), signIn: vi.fn(), sendAppFeedback: vi.fn() }))
vi.mock("@/lib/api/account", () => ({ ...auth, ACCOUNT_KEY: ["account"] }))
vi.mock("@/lib/api/feedback", () => ({ signIn: auth.signIn, sendAppFeedback: auth.sendAppFeedback }))
vi.mock("@/lib/desktop", () => ({ isDesktop: () => true }))
vi.mock("@/lib/api/update", () => ({
  dismissedVersion: () => null,
  checkUpdate: () => Promise.resolve(null),
  UPDATE_INTERVAL_MS: 60_000,
}))
vi.mock("@/lib/api/relay", () => ({ connectRelay: vi.fn() }))
vi.mock("@/lib/features", () => ({ useFeatures: () => ({ sets: false, auto_reply: false }) }))
vi.mock("@/hooks/useSetupDone", () => ({ useSetupDone: vi.fn(() => false) }))
vi.mock("@/pages/agent/Chats", () => ({ Chats: () => null }))
vi.mock("./RelayDot", () => ({ RelayDot: () => null }))

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/agent"]}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  auth.fetchAccount.mockResolvedValue({ signed_in: false, email: null, name: null, picture: null })
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("lists Setup until setup is done", async () => {
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "Setup" })).toBeTruthy())
  expect(screen.getByRole("link", { name: "Searches" })).toBeTruthy()
})

it("drops Setup once setup is done; Settings offers Repair setup instead", async () => {
  vi.mocked(useSetupDone).mockReturnValue(true)
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "Searches" })).toBeTruthy())
  expect(screen.queryByRole("link", { name: "Setup" })).toBeNull()
})
