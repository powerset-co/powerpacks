import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { isDesktop } from "@/lib/desktop"

import { SettingsPage } from "./SettingsPage"

vi.mock("@/lib/desktop", () => ({ isDesktop: vi.fn(() => false) }))
vi.mock("@/pages/install/Connect", () => ({
  Connect: ({ onDone }: { onDone?: () => void }) => onDone ? (
    <button type="button" onClick={onDone}>
      Done
    </button>
  ) : null,
}))

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/settings"]}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/people" element={<p>People page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** /api/features: the switches as saved, and a POST that saves what it is sent. */
function serveFeatures(saved: {
  sets: boolean
  auto_reply: boolean
  share: boolean
  support: boolean
  team: boolean
}) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (url === "/api/account")
      return Promise.resolve(new Response(JSON.stringify({ signed_in: true, email: "test@powerset.co" })))
    if (url === "/api/settings/providers")
      return Promise.resolve(new Response(JSON.stringify({ providers: [] })))
    if (init?.method === "POST")
      Object.assign(saved, JSON.parse(typeof init.body === "string" ? init.body : "{}"))
    return Promise.resolve(new Response(JSON.stringify(saved)))
  })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.mocked(isDesktop).mockReturnValue(false)
})

it("shows the remote assist switch as saved and flips it", async () => {
  vi.mocked(isDesktop).mockReturnValue(true)
  const saved = { sets: true, auto_reply: false, share: false, support: true, team: false }
  vi.stubGlobal("fetch", serveFeatures(saved))
  mount()
  const box = await screen.findByRole<HTMLInputElement>("checkbox", {
    name: "Let the Powerset team work on this install",
  })
  await waitFor(() => expect(box.checked).toBe(true))
  fireEvent.click(box)
  await waitFor(() => expect(saved.support).toBe(false))
  await waitFor(() => expect(box.checked).toBe(false))
})

it("offers Remote assist in a browser, and no Repair setup", async () => {
  vi.stubGlobal(
    "fetch",
    serveFeatures({ sets: false, auto_reply: false, share: false, support: false, team: false }),
  )
  mount()
  expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
  expect(
    await screen.findByRole("checkbox", { name: "Let the Powerset team work on this install" }),
  ).toBeTruthy()
  expect(screen.queryByRole("heading", { name: "Repair setup" })).toBeNull()
})

it("offers Repair setup without setup navigation buttons", () => {
  vi.mocked(isDesktop).mockReturnValue(true)
  mount()
  expect(screen.getByRole("heading", { name: "Repair setup" })).toBeTruthy()
  for (const name of ["Done", "Back", "Set up"])
    expect(screen.queryByRole("button", { name })).toBeNull()
})

it("keeps settings and account setup usable while provider status is loading", async () => {
  vi.mocked(isDesktop).mockReturnValue(true)
  const features = serveFeatures({ sets: false, auto_reply: false, share: false, support: true, team: false })
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      url === "/api/settings/providers"
        ? new Promise(() => {
            // Keep provider status pending while the rest of Settings renders.
          })
        : features(url),
    ),
  )
  mount()
  expect(await screen.findByLabelText("Loading API connections")).toBeTruthy()
  expect(screen.getByRole("heading", { name: "Repair setup" })).toBeTruthy()
  await waitFor(() =>
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Let the Powerset team work on this install" })
        .disabled,
    ).toBe(false),
  )
})

it("hides optional API keys for customer accounts", async () => {
  const fetch = vi.fn((url: string) =>
    Promise.resolve(
      new Response(
        JSON.stringify(url === "/api/account" ? { signed_in: true, email: "customer@example.com" } : {}),
      ),
    ),
  )
  vi.stubGlobal("fetch", fetch)
  mount()
  await screen.findByRole("checkbox", { name: "Let the Powerset team work on this install" })
  expect(screen.queryByRole("heading", { name: "API connections" })).toBeNull()
  expect(fetch.mock.calls.some(([url]) => url === "/api/settings/providers")).toBe(false)
})
