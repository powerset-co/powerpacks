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
const install = vi.hoisted(() => ({ fetchInstall: vi.fn() }))
vi.mock("@/lib/api/install", () => install)
const accounts = vi.hoisted(() => ({ fetchSignedIn: vi.fn() }))
vi.mock("@/lib/api/accounts", () => accounts)
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
  install.fetchInstall.mockResolvedValue({ status: "waiting", message: "Connect Gmail in your browser" })
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
  expect(screen.getByRole("link", { name: "People" })).toBeTruthy()
  // Searches heads the chats section instead of being a page link.
  expect(screen.queryByRole("link", { name: "Searches" })).toBeNull()
})

it("turns the Setup item while setup runs, the running step's line as its tip", async () => {
  install.fetchInstall.mockResolvedValue({ status: "running", message: "Building your search index" })
  mount()
  const setup = await screen.findByRole("link", { name: "Setup" })
  await waitFor(() => expect(setup.getAttribute("title")).toBe("Building your search index"))
  expect(setup.querySelector(".animate-spin")).toBeTruthy()
})

it("drops Setup once setup is done; Settings offers Repair setup instead", async () => {
  vi.mocked(useSetupDone).mockReturnValue(true)
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "People" })).toBeTruthy())
  expect(screen.queryByRole("link", { name: "Setup" })).toBeNull()
})

it("offers Add sources once setup is done with no message source in, and drops it once one is", async () => {
  vi.mocked(useSetupDone).mockReturnValue(true)
  accounts.fetchSignedIn.mockResolvedValue({
    linkedin: true,
    gmail: false,
    imessage: false,
    whatsapp: false,
    mailboxes: [],
  })
  mount()
  const link = await screen.findByRole("link", { name: "Add sources" })
  expect(link.getAttribute("href")).toBe("/onboarding/sources")
  cleanup()
  accounts.fetchSignedIn.mockResolvedValue({
    linkedin: true,
    gmail: true,
    imessage: false,
    whatsapp: false,
    mailboxes: ["casey@example.com"],
  })
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "People" })).toBeTruthy())
  expect(screen.queryByRole("link", { name: "Add sources" })).toBeNull()
})

it("does not offer Add sources while setup is not done", async () => {
  vi.mocked(useSetupDone).mockReturnValue(false)
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "Setup" })).toBeTruthy())
  expect(screen.queryByRole("link", { name: "Add sources" })).toBeNull()
})
