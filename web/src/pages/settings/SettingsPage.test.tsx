import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { SettingsPage } from "./SettingsPage"

let saved: { sets: boolean; auto_reply: boolean }
let posts: { url: string; body: unknown }[]
let finishSave: (() => void) | null

function mount(failSave = false) {
  saved = { sets: false, auto_reply: true }
  posts = []
  finishSave = null
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : null
        posts.push({ url, body })
        if (url === "/api/relay/connect")
          return Promise.resolve(new Response(JSON.stringify({ state: "offline" })))
        // The server answers with what it saved: what was sent.
        return new Promise<Response>((resolve) => {
          finishSave = () =>
            resolve(
              failSave
                ? new Response(JSON.stringify({ error: "disk full" }), { status: 500 })
                : new Response(typeof init.body === "string" ? init.body : null),
            )
        })
      }
      return Promise.resolve(new Response(JSON.stringify(saved)))
    }),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SettingsPage />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("turns Sets on from the click, keeps Auto Reply as it was, and wakes the relay", async () => {
  mount()
  const sets = await screen.findByRole("switch", { name: "Sets" })
  expect(sets.getAttribute("aria-checked")).toBe("false")
  fireEvent.click(sets)
  // On before the save answers.
  await waitFor(() => expect(sets.getAttribute("aria-checked")).toBe("true"))
  expect(posts).toEqual([{ url: "/api/features", body: { sets: true, auto_reply: true } }])
  finishSave?.()
  await waitFor(() => expect(posts.at(-1)).toEqual({ url: "/api/relay/connect", body: null }))
  expect(sets.getAttribute("aria-checked")).toBe("true")
})

it("puts the switch back and says why when the save fails", async () => {
  mount(true)
  const sets = await screen.findByRole("switch", { name: "Sets" })
  fireEvent.click(sets)
  await waitFor(() => expect(sets.getAttribute("aria-checked")).toBe("true"))
  finishSave?.()
  expect(await screen.findByRole("alert")).toBeTruthy()
  expect(sets.getAttribute("aria-checked")).toBe("false")
  expect(posts.map(({ url }) => url)).toEqual(["/api/features"])
})
