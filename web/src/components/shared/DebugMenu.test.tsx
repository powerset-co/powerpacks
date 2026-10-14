import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"

import { DebugMenu } from "./DebugMenu"

vi.mock("@/lib/desktop", () => ({ isDesktop: () => true, desktopPlatform: () => "macos" }))
vi.mock("@/lib/api/debug", () => ({
  fetchDebugState: () => Promise.resolve({ enabled: true, setupSkipped: false }),
  resetData: vi.fn(),
  skipSetup: vi.fn(),
}))

let posts: { url: string; body: unknown }[]

function mount(failSave = false) {
  posts = []
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : null
        posts.push({ url, body })
        if (url === "/api/relay/connect")
          return Promise.resolve(new Response(JSON.stringify({ state: "offline" })))
        // The server answers with what it saved: what was sent.
        return Promise.resolve(
          failSave
            ? new Response(JSON.stringify({ error: "disk full" }), { status: 500 })
            : new Response(typeof init.body === "string" ? init.body : null),
        )
      }
      return Promise.resolve(new Response(JSON.stringify({ sets: false, auto_reply: true })))
    }),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <DebugMenu />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("turns Sets on as a local feature, keeps Auto Reply as it was, and wakes the relay", async () => {
  mount()
  fireEvent.click(await screen.findByRole("button", { name: "Debug menu" }))
  const sets = await screen.findByRole("checkbox", { name: "Sets" })
  await waitFor(() => expect(sets.hasAttribute("disabled")).toBe(false))
  fireEvent.click(sets)
  await waitFor(() => expect(posts.at(-1)).toEqual({ url: "/api/relay/connect", body: null }))
  expect(posts[0]).toEqual({ url: "/api/features", body: { sets: true, auto_reply: true } })
})

it("says why when Sets could not be saved, and wakes nothing", async () => {
  mount(true)
  fireEvent.click(await screen.findByRole("button", { name: "Debug menu" }))
  fireEvent.click(await screen.findByRole("checkbox", { name: "Sets" }))
  expect(await screen.findByRole("alert")).toBeTruthy()
  expect(posts.map(({ url }) => url)).toEqual(["/api/features"])
})
