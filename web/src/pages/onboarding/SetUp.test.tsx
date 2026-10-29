import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import type { SignedIn } from "@/lib/api/accounts"
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

const NONE: SignedIn = { linkedin: false, gmail: false, imessage: false, whatsapp: false, mailboxes: [] }
const LINKEDIN: SignedIn = { ...NONE, linkedin: true }

let state: { record: InstallStatus; signed: SignedIn; signedStatus: number }

beforeEach(() => {
  invoke.mockClear()
  state = { record: STOPPED_ON_SOURCES, signed: LINKEDIN, signedStatus: 200 }
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      Promise.resolve(
        url === "/api/install"
          ? new Response(JSON.stringify(state.record), { status: 200 })
          : url === "/accounts/api/signed-in"
            ? new Response(
                JSON.stringify(
                  state.signedStatus === 200 ? state.signed : { error: "msgvault is not readable" },
                ),
                { status: state.signedStatus },
              )
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
          <Route path="/onboarding/linkedin" element={<h1>Connect LinkedIn</h1>} />
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

it("starts setup by itself once a source is signed in", async () => {
  state.record = WAITING
  mount()
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("onboard_continue", { answer: {} }))
})

it("with nothing signed in it starts nothing and goes back to the LinkedIn step", async () => {
  // A run now would skip every source and call Powerpacks installed with no one in it.
  state = { record: WAITING, signed: NONE, signedStatus: 200 }
  mount()
  await screen.findByRole("heading", { name: "Connect LinkedIn" })
  expect(invoke).not.toHaveBeenCalled()
})

it("a sign-in check that fails is shown, not swallowed", async () => {
  state = { record: WAITING, signed: NONE, signedStatus: 500 }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/onboarding/setup"]}>
        <Routes>
          <Route path="/onboarding/:step" element={<SetUp />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await screen.findByText(/msgvault is not readable/)
  expect(invoke).not.toHaveBeenCalled()
})

it("a record a run is writing between steps is the run's, not a prompt to connect", async () => {
  // sources.selected: status completed on the sources step, carrying the run's pid. Nothing is
  // signed in yet from this screen's point of view, but the run owns the record.
  state = {
    record: {
      ...WAITING,
      status: "completed",
      installer_pid: 69310,
      step: "sources",
      event: "sources.selected",
      message: "Sources selected",
    },
    signed: NONE,
    signedStatus: 200,
  }
  mount()
  await screen.findByText("Sources selected")
  expect(screen.queryByRole("heading", { name: "Connect LinkedIn" })).toBeNull()
  expect(invoke).not.toHaveBeenCalled()
})
