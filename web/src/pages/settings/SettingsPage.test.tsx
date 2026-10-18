import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { isDesktop } from "@/lib/desktop"

import { SettingsPage } from "./SettingsPage"

vi.mock("@/lib/desktop", () => ({ isDesktop: vi.fn(() => false) }))
vi.mock("@/pages/install/Connect", () => ({
  Connect: ({ onDone }: { onDone: () => void }) => (
    <button type="button" onClick={onDone}>
      Done
    </button>
  ),
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
  return vi.fn((_url: string, init?: RequestInit) => {
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

it("offers Repair setup in the desktop app: the Connect list, Done going home", () => {
  vi.mocked(isDesktop).mockReturnValue(true)
  mount()
  expect(screen.getByRole("heading", { name: "Repair setup" })).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Done" }))
  expect(screen.getByText("People page")).toBeTruthy()
})
