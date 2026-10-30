import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
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
  prose: { page: {} },
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
  // The first look at ready watches its size; jsdom has no layout.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {
        /* No browser layout in jsdom. */
      }
      disconnect() {
        /* No browser layout in jsdom. */
      }
    },
  )
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
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/onboarding/setup"]}>
        <Routes>
          <Route path="/onboarding/:step" element={<SetUp />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

/** The rows on screen: each one's standing and text. */
function rows() {
  return within(screen.getByRole("list", { name: "Setup stages" }))
    .getAllByRole("listitem")
    .map((row) => [row.getAttribute("data-stage"), row.textContent])
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
  // Try again sends no choice: the app runs the run the record names again.
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

// The records linkedin_setup.py writes (tests/test_install_linkedin_setup.py), as GET /api/install
// returns them.
const LINKEDIN_PLAN = ["linkedin", "index", "validate", "ready"]
const READING: InstallStatus = {
  ...WAITING,
  updated_at: new Date().toISOString(),
  status: "running",
  installer_pid: 4242,
  step: "linkedin",
  event: "linkedin.reading.count",
  message: "Reading your LinkedIn connections: 120 of 1,075",
  retry_command: "bin/linkedin-setup",
  plan: LINKEDIN_PLAN,
  steps: { linkedin: { status: "running", message: "Reading your LinkedIn connections: 120 of 1,075" } },
  action: { kind: "details", details: { read: 120, total: 1075 } },
}

it("a LinkedIn-only run lists its two rows, Modal's words with a count and time left, then ready", async () => {
  state.record = READING
  const client = mount()
  await screen.findByRole("heading", { name: "Reading your connections" })
  expect(rows()).toEqual([
    ["current", `Reading your connections${READING.message}`],
    ["next", "Making your connections searchable"],
  ])
  // The line carries the count in words; one read gives no pace yet, so no time left.
  expect(screen.getByText(READING.message)).toBeTruthy()
  expect(screen.queryByRole("progressbar")).toBeNull()

  const read = { status: "completed", message: "1,075 LinkedIn connections (12 new)" } as const
  state.record = {
    ...READING,
    step: "index",
    event: "index.building",
    message: "Understanding companies",
    steps: { linkedin: read, index: { status: "running", message: "Understanding companies" } },
    action: { kind: "details", details: { done: 40, total: 111, left_seconds: 600 } },
  }
  await act(() => client.invalidateQueries())
  await screen.findByRole("heading", { name: "Making your connections searchable" })
  expect(rows()).toEqual([
    ["done", "1,075 LinkedIn connections (12 new)"],
    ["current", "Making your connections searchableUnderstanding companiesabout 10 min left"],
  ])
  expect(screen.getByText("Understanding companies")).toBeTruthy()
  expect(screen.getByText("about 10 min left")).toBeTruthy()
  expect(screen.queryByRole("progressbar")).toBeNull()

  const done = (message: string) => ({ status: "completed", message }) as const
  state.record = {
    ...READING,
    installer_pid: 0,
    status: "completed",
    step: "ready",
    event: "search.ready",
    message: "Search is ready: 1,075 people searchable.",
    steps: {
      linkedin: read,
      index: done("Your search index is built"),
      validate: done("Search is ready: 1,075 people searchable."),
      ready: done("Search is ready: 1,075 people searchable."),
    },
    action: null,
  }
  await act(() => client.invalidateQueries())
  await screen.findByRole("heading", { name: "Search is ready" })
  expect(rows()).toEqual([
    ["done", "1,075 LinkedIn connections (12 new)"],
    ["done", "Search is ready: 1,075 people searchable."],
  ])
})

it("an import run lists every row in its plan, deep context with its count", async () => {
  const plan = [
    "account",
    "sources",
    "linkedin_login",
    "gmail_tools",
    "gmail_login",
    "imessage_access",
    "whatsapp_tools",
    "whatsapp_login",
    "linkedin",
    "gmail_sync",
    "gmail_import",
    "imessage_import",
    "whatsapp_sync",
    "whatsapp_import",
    "deep_context",
    "enrich",
    "index",
    "validate",
    "ready",
  ]
  const before = plan.slice(0, plan.indexOf("deep_context"))
  state.record = {
    ...READING,
    step: "deep_context",
    event: "deep_context.learning",
    message: "Learning about your contacts: 40 of 298",
    plan,
    steps: {
      ...Object.fromEntries(before.map((step) => [step, { status: "completed", message: `${step} done` }])),
      deep_context: { status: "running", message: "Learning about your contacts: 40 of 298" },
    },
    action: { kind: "details", details: { done: 40, total: 298 } },
  }
  mount()
  await screen.findByRole("heading", { name: "Learning who they are" })
  expect(rows()).toEqual([
    ["done", "whatsapp_login done"],
    ["done", "linkedin done"],
    ["done", "gmail_import done"],
    ["done", "imessage_import done"],
    ["done", "whatsapp_import done"],
    ["current", "Learning who they areLearning about your contacts: 40 of 298"],
    ["next", "Enriching people"],
    ["next", "Making your connections searchable"],
  ])
  expect(screen.getByText("Learning about your contacts: 40 of 298")).toBeTruthy()
  expect(screen.queryByRole("progressbar")).toBeNull()
  expect(screen.getByRole("button", { name: "Start using Powerpacks now" })).toBeTruthy()
})
