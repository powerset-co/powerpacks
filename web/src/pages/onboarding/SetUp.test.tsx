import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import type { InstallStatus } from "@/types/install"

import { SetUp } from "./SetUp"

const invoke = vi.fn((_command: string, _args?: unknown) => Promise.resolve(null))
vi.mock("@/lib/desktop", () => ({
  invoke: (command: string, args?: unknown) => invoke(command, args),
  isDesktop: () => false,
}))

// The record setup leaves after it stops on its first step (sources: the sign-ins it reads before
// LinkedIn), as GET /api/install returns it. `sources` belongs to no stage the page lists.
const STOPPED_ON_SOURCES: InstallStatus = {
  updated_at: "2026-10-28T00:41:33Z",
  primitive: "powerpacks_install",
  status: "failed",
  installer_pid: 47839,
  step: "sources",
  event: "step.failed",
  message: "This step stopped. Try again to pick up from here.",
  note: "",
  log_path: "/Users/casey/.powerpacks/app/.powerpacks/install/install.log",
  retry_command: "/Users/casey/.powerpacks/app/bin/onboard --source linkedin",
  steps: {
    account: { status: "skipped", message: "Already signed in as casey@example.com." },
    sources: { status: "failed", message: "This step stopped. Try again to pick up from here." },
  },
  prose: { rows: [], page: {} },
  action: { kind: "error", details: { error: "no such table: sources", error_type: "OperationalError" } },
}

// The record a launch leaves when nothing runs: setup waits for a click (boot.rs record_install).
const WAITING: InstallStatus = {
  ...STOPPED_ON_SOURCES,
  status: "waiting",
  installer_pid: 0,
  step: "skills",
  event: "setup.ready",
  message: "Ready to set up: sign in, import your contacts and build your network.",
  steps: { account: { status: "skipped", message: "Already signed in as casey@example.com." } },
  action: null,
}

let state: { record: InstallStatus }

beforeEach(() => {
  invoke.mockClear()
  state = { record: STOPPED_ON_SOURCES }
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      Promise.resolve(
        url === "/api/install"
          ? new Response(JSON.stringify(state.record), { status: 200 })
          : new Response(JSON.stringify({ error: url }), { status: 404 }),
      ),
    ),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/onboarding/setup"]}>
        <Routes>
          <Route path="/onboarding/:step" element={<SetUp />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it("a stop before any stage shows the stop and Try again, and does not restart by itself", async () => {
  mount()
  await screen.findByRole("heading", { name: "Setup stopped" })
  expect(screen.getByText("This step stopped. Try again to pick up from here.")).toBeTruthy()
  expect(screen.getByText("no such table: sources")).toBeTruthy()
  expect(screen.getByText(/Logs are in/).textContent).toContain(
    "/Users/casey/.powerpacks/app/.powerpacks/install/",
  )
  expect(invoke).not.toHaveBeenCalled()

  fireEvent.click(screen.getByRole("button", { name: "Try again" }))
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
  expect(screen.getByRole<HTMLButtonElement>("button", { name: /Starting/ }).disabled).toBe(true)
})

it("an idle record starts nothing", async () => {
  // The record a launch leaves: setup waits for the person's click on LinkedIn's Continue.
  state.record = WAITING
  mount()
  await screen.findByText(WAITING.message)
  expect(invoke).not.toHaveBeenCalled()
})

it("the index row shows the Modal run's counts as a bar", async () => {
  state.record = {
    ...WAITING,
    status: "running",
    installer_pid: 4242,
    step: "index",
    event: "index.building",
    message: "Building your search index",
    steps: { linkedin: { status: "completed", message: "1,075 LinkedIn connections" } },
    index_progress: {
      status: "running",
      message: "Company profiles: 40 of 111",
      progress: 0.36,
      payload: { done: 40, total: 111 },
    },
  }
  mount()
  await screen.findByText("Company profiles: 40 of 111")
  expect(screen.getByRole("progressbar")).toBeTruthy()
})
