import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeAll, expect, it, vi } from "vitest"

import { ChromeProfile } from "./ChromeProfile"

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe = vi.fn()
      disconnect = vi.fn()
    },
  )
  vi.stubGlobal("matchMedia", () => ({ matches: true }))
})

afterEach(() => {
  cleanup()
})

const json = (value: unknown) =>
  new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } })

function show(profiles: unknown[]) {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve(json(url.endsWith("profiles") ? { profiles } : { status: "copied" })),
  )
  vi.stubGlobal("fetch", fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/onboarding/chrome"]}>
        <Routes>
          <Route path="/onboarding/chrome" element={<ChromeProfile />} />
          <Route path="/onboarding/signup" element={<p>sign up</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return fetchMock
}

it("copies the profile the user picks, then moves on to sign up", async () => {
  const fetchMock = show([
    { browser: "chrome", folder: "Default", name: "Person 1", email: "a@example.com", linkedin: true, google: true },
  ])
  fireEvent.click(await screen.findByRole("button", { name: /a@example.com/ }))
  await screen.findByText("sign up")
  expect(fetchMock).toHaveBeenCalledWith(
    "/accounts/api/chrome-profile",
    expect.objectContaining({ method: "POST" }),
  )
})

it("goes straight to sign up without a profile to pick", async () => {
  show([])
  await screen.findByText("sign up")
})

it("moves on without copying when the user starts empty", async () => {
  const fetchMock = show([{ browser: "chrome", folder: "Default", name: "Person 1", email: "", linkedin: true, google: false }])
  fireEvent.click(await screen.findByRole("button", { name: "Start empty and sign in myself" }))
  await screen.findByText("sign up")
  expect(fetchMock).not.toHaveBeenCalledWith("/accounts/api/chrome-profile", expect.anything())
})

it("shows each profile's browser and copies from the one picked", async () => {
  const fetchMock = show([
    { browser: "chrome", folder: "Default", name: "Person 1", email: "a@example.com", linkedin: true, google: false },
    { browser: "brave", folder: "Default", name: "Person 2", email: "b@example.com", linkedin: false, google: true },
  ])
  expect(await screen.findByAltText("Chrome")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: /b@example.com/ }))
  await screen.findByText("sign up")
  const [, init] = fetchMock.mock.calls.find(([url]) => url === "/accounts/api/chrome-profile") as unknown as [
    string,
    RequestInit,
  ]
  expect(String(init.body)).toContain("brave")
})
