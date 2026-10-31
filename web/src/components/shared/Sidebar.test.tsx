import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, useLocation } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

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
vi.mock("@/pages/agent/Chats", () => ({ Chats: () => null }))
vi.mock("./RelayDot", () => ({ RelayDot: () => null }))

function Path() {
  return <span data-testid="path">{useLocation().pathname}</span>
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/agent"]}>
        <Sidebar />
        <Path />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  auth.fetchAccount.mockResolvedValue({ signed_in: false, email: null, name: null, picture: null })
  install.fetchInstall.mockResolvedValue({
    status: "waiting",
    message: "Connect Gmail in your browser",
    steps: {},
  })
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }))
})
afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

it("lists the pages, Searches heading the chats instead of a page link", async () => {
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "People" })).toBeTruthy())
  expect(screen.queryByRole("link", { name: "Searches" })).toBeNull()
  expect(screen.queryByRole("link", { name: "Setup" })).toBeNull()
})

it("shows the import while it runs, with no dismiss, opening the Import screen", async () => {
  install.fetchInstall.mockResolvedValue({
    status: "running",
    step: "index",
    message: "Building your search index",
    updated_at: "2026-10-30T07:32:12Z",
    steps: {},
  })
  mount()
  expect(await screen.findByText("Importing")).toBeTruthy()
  expect(screen.getByText("Building your search index")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Dismiss this import" })).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: "View in Accounts" }))
  await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/accounts"))
})

it("keeps a stopped import, with no dismiss", async () => {
  install.fetchInstall.mockResolvedValue({
    status: "failed",
    step: "gmail_sync",
    message: "Gmail sync stopped",
    updated_at: "2026-10-30T07:32:12Z",
    steps: {},
  })
  mount()
  expect(await screen.findByText("Import stopped")).toBeTruthy()
  expect(screen.queryByRole("button", { name: "Dismiss this import" })).toBeNull()
})

it("lets a finished import be dismissed, and stays dismissed for that run", async () => {
  install.fetchInstall.mockResolvedValue({
    status: "completed",
    step: "ready",
    message: "Search is ready: 378 people searchable.",
    updated_at: "2026-10-30T07:37:30Z",
    steps: {},
  })
  mount()
  expect(await screen.findByText("Import done")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Dismiss this import" }))
  expect(screen.queryByText("Import done")).toBeNull()
  cleanup()
  mount()
  await waitFor(() => expect(screen.getByRole("link", { name: "People" })).toBeTruthy())
  expect(screen.queryByText("Import done")).toBeNull()
})
