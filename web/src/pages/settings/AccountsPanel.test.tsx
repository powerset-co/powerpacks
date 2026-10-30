import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter, useLocation } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { InstallStatus } from "@/types/install"
import { AccountsSettings } from "./AccountsSettings"

vi.mock("@/lib/agent/useCodexAccount", () => ({
  useCodexAccount: () => ({
    status: { installed: true, account: null },
    loading: false,
    login: null,
    connect: vi.fn(),
  }),
}))
const idle: InstallStatus = {
  primitive: "powerpacks_install",
  updated_at: "2026-10-01T00:00:00Z",
  status: "completed",
  step: "ready",
  event: "search.ready",
  installer_pid: 0,
  message: "Search is ready",
  plan: ["linkedin", "index", "ready"],
  steps: {},
  log_path: "/tmp/example.log",
  retry_command: "bin/onboard",
  prose: { page: {} },
}
let record: InstallStatus, connected: boolean, invoke: ReturnType<typeof vi.fn>
beforeEach(() => {
  record = idle
  connected = true
  invoke = vi.fn(() => Promise.resolve(null))
  vi.stubGlobal("__TAURI__", { core: { invoke }, event: { listen: vi.fn(() => Promise.resolve(vi.fn())) } })
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe = vi.fn()
      disconnect = vi.fn()
    },
  )
  vi.stubGlobal("matchMedia", () => ({ matches: true }))
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const payload =
        url === "/api/account"
          ? { signed_in: true, email: "jordan@example.com", name: "Jordan" }
          : url === "/api/install"
            ? record
            : url === "/api/install/connect"
              ? { ...idle, event: "setup.ready", step: "runtime", status: "waiting" }
              : url === "/accounts/api/signed-in"
                ? {
                    linkedin: connected,
                    gmail: connected,
                    imessage: false,
                    whatsapp: false,
                    mailboxes: connected ? ["jordan@example.com"] : [],
                  }
                : url === "/accounts/api/accounts"
                  ? {
                      accounts: connected
                        ? [
                            {
                              source: "gmail",
                              name: "jordan@example.com",
                              health: "ok",
                              note: "Up to date",
                              messages: 120,
                              contacts: 40,
                              latest_message_at: null,
                              last_sync_at: null,
                            },
                          ]
                        : [],
                      jobs: {},
                      scheduled: true,
                    }
                  : {}
      return Promise.resolve(new Response(JSON.stringify(payload)))
    }),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
function Path() {
  const at = useLocation()
  return (
    <output data-testid="path">
      {at.pathname}
      {at.search}
    </output>
  )
}
function mount(path = "/accounts") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AccountsSettings panel />
        <Path />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}
it("keeps connected details and the disconnected helper in one Accounts screen", async () => {
  mount()
  await screen.findByRole("heading", { name: "Bring your conversations along." })
  fireEvent.click(screen.getByRole("button", { name: /Gmail/ }))
  expect(await screen.findByText("120")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Add another" })).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: /WhatsApp/ }))
  expect(await screen.findByRole("heading", { name: "Link your WhatsApp." })).toBeTruthy()
  expect(screen.getByTestId("path").textContent).toBe("/accounts")
  expect(screen.queryByRole("button", { name: "View sync progress" })).toBeNull()
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
})
it("refreshes all sources then shows Deep Context progress without leaving Accounts", async () => {
  const client = mount()
  const start = await screen.findByRole<HTMLButtonElement>("button", { name: "Resync all" })
  await waitFor(() => expect(start.disabled).toBe(false))
  fireEvent.click(start)
  await waitFor(() =>
    expect(invoke).toHaveBeenCalledWith("onboard_continue", {
      answer: { sources: ["linkedin", "gmail", "imessage", "whatsapp"], refresh: true },
    }),
  )
  expect(screen.getByTestId("path").textContent).toBe("/accounts?view=sync")
  expect(screen.queryByText("Search is ready")).toBeNull()
  record = {
    ...idle,
    updated_at: idle.updated_at,
    status: "running",
    event: "deep_context.learning",
    step: "deep_context",
    installer_pid: 123,
    plan: ["gmail_import", "deep_context", "index"],
    steps: { gmail_import: { status: "completed", message: "Gmail imported" } },
    message: "Learning about your contacts: 40 of 298",
    action: { kind: "details", details: { done: 40, total: 298 } },
  }
  await client.invalidateQueries({ queryKey: ["onboarding", "install"] })
  expect(await screen.findByText(record.message)).toBeTruthy()
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Syncing…" }).disabled).toBe(true)
  expect(within(screen.getByRole("list", { name: "Setup stages" })).getByText("Gmail imported")).toBeTruthy()
})
it("disables resync when no sources are connected", async () => {
  connected = false
  mount()
  await screen.findByRole("heading", { name: "Bring your conversations along." })
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Resync all" }).disabled).toBe(true)
})
it("keeps owner input and stopped-run recovery on Accounts", async () => {
  record = {
    ...idle,
    status: "waiting",
    event: "step.waiting",
    step: "owner",
    action: { kind: "owner" },
    message: "Your LinkedIn profile is needed",
  }
  mount("/accounts?view=sync")
  expect(await screen.findByRole("textbox", { name: "Your LinkedIn profile" })).toBeTruthy()
  fireEvent.change(screen.getByRole("textbox", { name: "Your LinkedIn profile" }), {
    target: { value: "https://www.linkedin.com/in/jordan-example" },
  })
  fireEvent.click(screen.getByRole("button", { name: "Continue" }))
  await waitFor(() =>
    expect(invoke).toHaveBeenCalledWith("onboard_continue", {
      answer: { linkedinUrl: "https://www.linkedin.com/in/jordan-example" },
    }),
  )
  expect(screen.getByTestId("path").textContent).toBe("/accounts?view=sync")
  expect(screen.queryByRole("button", { name: "Continue" })).toBeNull()
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Syncing…" }).disabled).toBe(true)
})
