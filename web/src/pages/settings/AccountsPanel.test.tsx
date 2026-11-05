import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter, useLocation } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { writeStored } from "@/lib/storage"
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
let jobs: Record<string, { state: "running" | "done" | "failed"; step: string }>
beforeEach(() => {
  record = idle
  connected = true
  jobs = {}
  // Accounts has been opened before, so How Deep Context works stays closed.
  writeStored("local", "accounts.howItWorks", true)
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
  // How Deep Context works plays on the Web Animations API, which jsdom lacks: its moves never end.
  vi.stubGlobal("CSS", { supports: () => true })
  Object.defineProperty(Element.prototype, "animate", {
    configurable: true,
    value: () => ({ finished: new Promise(() => undefined), cancel: () => undefined }),
  })
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
                      jobs,
                      scheduled: "daily at 6:00 AM",
                    }
                  : {}
      return Promise.resolve(new Response(JSON.stringify(payload)))
    }),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  Reflect.deleteProperty(Element.prototype, "animate")
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
it("opens How Deep Context works on the first visit to Accounts only, and its link opens it again", async () => {
  localStorage.clear()
  mount()
  const dialog = screen.getByRole("dialog", { name: "How Deep Context works" })
  expect(within(dialog).getByRole("heading", { name: "Your messages stay on your Mac" })).toBeTruthy()
  fireEvent.click(within(dialog).getByRole("button", { name: "Step 4: Only the professional picture" }))
  expect(within(dialog).getByRole("heading", { name: "Only the professional picture" })).toBeTruthy()
  fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }))
  expect(screen.queryByRole("dialog")).toBeNull()
  cleanup()
  mount()
  expect(screen.queryByRole("dialog")).toBeNull()
  fireEvent.click(await screen.findByRole("button", { name: "How your data is handled" }))
  expect(screen.getByRole("dialog", { name: "How Deep Context works" })).toBeTruthy()
})
it("keeps connected details and the disconnected helper in one Accounts screen", async () => {
  mount()
  await screen.findByRole("heading", { name: "Bring your conversations along." })
  fireEvent.click(screen.getByRole("button", { name: /Gmail/ }))
  expect(await screen.findByText("120")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Add a mailbox" })).toBeTruthy()
  // Connected: its details, never its connect guide.
  expect(screen.queryByRole("heading", { name: "Connect your Gmail." })).toBeNull()
  expect(screen.queryByRole("button", { name: "View connection guide" })).toBeNull()
  fireEvent.click(screen.getByRole("button", { name: /WhatsApp/ }))
  expect(await screen.findByRole("heading", { name: "Link your WhatsApp." })).toBeTruthy()
  expect(screen.getByTestId("path").textContent).toBe("/accounts")
  expect(screen.queryByRole("button", { name: "view progress" })).toBeNull()
  expect(invoke).not.toHaveBeenCalledWith("onboard_continue", expect.anything())
})
it("connects only: no Enrich or Sync, and the sync line points to Scheduled tasks", async () => {
  mount()
  // The schedule is part of the header's one statement, linking to Scheduled tasks.
  const tasks = await screen.findByRole("link", { name: "daily at 6:00 AM" })
  expect(tasks.getAttribute("href")).toBe("/tasks")
  expect(screen.queryByRole("button", { name: "Enrich imported data" })).toBeNull()
  expect(screen.queryByRole("button", { name: "Sync" })).toBeNull()
})
it("while a run goes, says so and opens its progress on Accounts", async () => {
  record = {
    ...idle,
    updated_at: new Date().toISOString(),
    status: "running",
    event: "deep_context.learning",
    step: "deep_context",
    installer_pid: 123,
    plan: ["gmail_import", "deep_context", "index"],
    steps: { gmail_import: { status: "completed", message: "Gmail imported" } },
    message: "Learning about your contacts: 40 of 298",
    action: { kind: "details", details: { done: 40, total: 298 } },
  }
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "view progress" }))
  expect(screen.getByTestId("path").textContent).toBe("/accounts?view=sync")
  expect(await screen.findByText(record.message)).toBeTruthy()
  expect(within(screen.getByRole("list", { name: "Setup stages" })).getByText("Gmail imported")).toBeTruthy()
})
it("never shows onboarding's LinkedIn setup as the run's progress", async () => {
  record = { ...idle, retry_command: "/app/bin/linkedin-setup" }
  mount("/accounts?view=sync")
  await screen.findByRole("link", { name: "daily at 6:00 AM" })
  expect(screen.queryByText("Search is ready")).toBeNull()
  expect(screen.queryByRole("list", { name: "Setup stages" })).toBeNull()
})
it("lists no row after the running one as done from the last run", async () => {
  record = {
    ...idle,
    updated_at: new Date().toISOString(),
    status: "running",
    event: "account.checking",
    step: "account",
    installer_pid: 123,
    plan: ["account", "linkedin", "gmail_import", "deep_context", "index", "ready"],
    // What the last run left: its source and processing steps clear once this run selects sources.
    steps: {
      linkedin: { status: "completed", message: "Read 400 connections" },
      deep_context: { status: "completed", message: "Learned about 298 people" },
    },
    message: "Checking your account",
  }
  mount("/accounts?view=sync")
  const stages = await screen.findByRole("list", { name: "Setup stages" })
  expect(within(stages).queryByText("Read 400 connections")).toBeNull()
  expect(within(stages).queryByText("Learned about 298 people")).toBeNull()
  expect(stages.querySelectorAll('[data-stage="done"]').length).toBe(0)
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
})
