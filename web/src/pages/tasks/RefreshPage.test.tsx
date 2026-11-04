import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { RefreshPane } from "@/components/shared/RefreshPane"
import type { RefreshRun, RefreshState } from "@/types/refresh"

import { pick } from "@/testing/select"

import { clock, when } from "./format"
import { RefreshPage } from "./RefreshPage"

const run: RefreshRun = {
  id: 7,
  trigger: "daily",
  status: "needs_you",
  started_at: "2026-11-03T06:00:04-08:00",
  finished_at: "2026-11-03T06:11:20-08:00",
  pid: 0,
  missed_days: 1,
  sources: [
    { source: "gmail", status: "skipped", accounts: [] },
    { source: "whatsapp", status: "synced", accounts: [] },
  ],
  people_before: 12,
  people_after: 26,
  index_rebuilt: true,
  problems: [
    {
      source: "gmail",
      title: "Gmail sign-in expired for casey@example.com",
      detail: "Reconnect it on Accounts; the next refresh picks up all the mail since.",
      action: "Reconnect Gmail",
      href: "/accounts?source=gmail",
    },
    {
      source: "whatsapp",
      title: "WhatsApp unlinked this Mac",
      detail: "Scan the QR code again on Accounts to keep WhatsApp in sync.",
      action: "Link WhatsApp",
      href: "/accounts?source=whatsapp",
    },
  ],
  message: "Search is ready.",
  error: "",
  log: "",
}

const state: RefreshState = {
  settings: { enabled: false, time: "06:00", label: "6:00 AM" },
  next_run_at: null,
  running: null,
  runs: [run],
}

function mount(element: React.ReactElement) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  )
}

function serve(answer: (url: string, options?: RequestInit) => RefreshState) {
  const fetch = vi.fn((url: string, options?: RequestInit) =>
    Promise.resolve(new Response(JSON.stringify(answer(url, options)), { status: 200 })),
  )
  vi.stubGlobal("fetch", fetch)
  return fetch
}

/** The settings form the page posted. */
function posted(fetch: { mock: { calls: [string, RequestInit?][] } }): string {
  const sent = fetch.mock.calls.find(([url]) => url === "/tasks/api/refresh/settings")?.[1]?.body
  return sent instanceof URLSearchParams ? sent.toString() : ""
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

it("turns the daily refresh on before the server answers, then saves it", async () => {
  let answer: (response: Response) => void = () => undefined
  const fetch = vi.fn((_url: string, options?: RequestInit) =>
    options?.method === "POST"
      ? new Promise<Response>((resolve) => (answer = resolve))
      : Promise.resolve(new Response(JSON.stringify(state))),
  )
  vi.stubGlobal("fetch", fetch)
  mount(<RefreshPage />)
  const toggle = await screen.findByRole("switch", { name: "Refresh every day" })
  expect(toggle.getAttribute("aria-checked")).toBe("false")
  fireEvent.click(toggle)
  await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"))
  expect(posted(fetch)).toBe("enabled=true")
  answer(
    new Response(
      JSON.stringify({
        ...state,
        settings: { ...state.settings, enabled: true },
        next_run_at: "2026-11-04T06:00:00-08:00",
      }),
    ),
  )
  expect(await screen.findByText(/Next run/)).toBeTruthy()
  expect(toggle.getAttribute("aria-checked")).toBe("true")
})

it("changes the time from the picker", async () => {
  const fetch = serve((_url, options) =>
    options?.method === "POST"
      ? { ...state, settings: { enabled: false, time: "07:00", label: "7:00 AM" } }
      : state,
  )
  mount(<RefreshPage />)
  await screen.findByRole("combobox", { name: "Time of day" })
  pick("Time of day", "7:00 AM")
  await waitFor(() => {
    expect(posted(fetch)).toBe("time=07%3A00")
  })
})

it("starts a refresh now and shows it running", async () => {
  const running = { ...run, id: 8, status: "running" as const, finished_at: "", problems: [] }
  serve((_url, options) => (options?.method === "POST" ? { ...state, running, runs: [running, run] } : state))
  mount(<RefreshPage />)
  fireEvent.click(await screen.findByRole("button", { name: "Refresh now" }))
  expect(await screen.findByRole("button", { name: /Refreshing/ })).toHaveProperty("disabled", true)
  expect(screen.getByRole("link", { name: "See progress" }).getAttribute("href")).toBe("/accounts?view=sync")
})

it("lists runs with people added and what to do", async () => {
  serve(() => state)
  mount(<RefreshPage />)
  expect(await screen.findByText("+14")).toBeTruthy()
  expect(screen.getByText("Daily · caught up 2 days")).toBeTruthy()
  expect(screen.getByText("Rebuilt")).toBeTruthy()
  // A skipped source is a yellow warning badge, a synced one a green check, never struck through.
  expect(screen.getByTitle("Gmail: Skipped: not signed in").dataset.sourceStatus).toBe("skipped")
  expect(screen.getByTitle("WhatsApp: Synced").dataset.sourceStatus).toBe("synced")
  expect(screen.getByRole("link", { name: "Reconnect Gmail" }).getAttribute("href")).toBe(
    "/accounts?source=gmail",
  )
})

it("the sidebar pane says what to do and stays dismissed for that run", async () => {
  serve(() => state)
  mount(<RefreshPane rail={false} />)
  expect(await screen.findByText("Gmail sign-in expired for casey@example.com")).toBeTruthy()
  expect(screen.getByRole("button", { name: "Reconnect Gmail" })).toBeTruthy()
  expect(screen.getByText(/\+1 more: WhatsApp unlinked this Mac/)).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }))
  expect(screen.queryByText("Gmail sign-in expired for casey@example.com")).toBeNull()
  cleanup()
  mount(<RefreshPane rail={false} />)
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull())
})

it("formats times the way the page reads", () => {
  expect(clock("06:00")).toBe("6:00 AM")
  expect(clock("18:30")).toBe("6:30 PM")
  const now = new Date(2026, 10, 3, 12)
  expect(when(new Date(2026, 10, 3, 6).toISOString(), now)).toBe("Today 6:00 AM")
  expect(when(new Date(2026, 10, 4, 6).toISOString(), now)).toBe("Tomorrow 6:00 AM")
})
