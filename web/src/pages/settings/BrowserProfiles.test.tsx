import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { BrowserProfiles } from "./BrowserProfiles"

afterEach(() => {
  cleanup()
})

const json = (value: unknown) =>
  new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } })

const PROFILES = [
  { folder: "Default", name: "Person 1", email: "a@example.com", linkedin: true, google: true },
  { folder: "Profile 1", name: "Work", email: "b@example.com", linkedin: false, google: true },
]

it("checks the linked profile and copies another one picked", async () => {
  let linked = "Default"
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") linked = "Profile 1"
    return Promise.resolve(
      json(url.endsWith("profiles") ? { profiles: PROFILES, linked } : { status: "copied" }),
    )
  })
  vi.stubGlobal("fetch", fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <BrowserProfiles />
    </QueryClientProvider>,
  )
  const first = await screen.findByRole("button", { name: /a@example.com/ })
  expect(first.getAttribute("aria-pressed")).toBe("true")
  fireEvent.click(screen.getByRole("button", { name: /b@example.com/ }))
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /b@example.com/ }).getAttribute("aria-pressed")).toBe("true"),
  )
  expect(fetchMock).toHaveBeenCalledWith(
    "/accounts/api/chrome-profile",
    expect.objectContaining({ method: "POST" }),
  )
})
