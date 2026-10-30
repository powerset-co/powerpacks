import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { ChromeProfiles } from "./ChromeProfiles"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const json = (value: unknown) =>
  new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } })

function show(profiles: unknown[], onUsed = vi.fn()) {
  const fetchMock = vi.fn((url: string) => Promise.resolve(json(url.endsWith("profiles") ? { profiles } : { status: "copied" })))
  vi.stubGlobal("fetch", fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ChromeProfiles onUsed={onUsed} />
    </QueryClientProvider>,
  )
  return fetchMock
}

it("copies the profile the user picks", async () => {
  const onUsed = vi.fn()
  const fetchMock = show([{ folder: "Default", name: "Person 1", email: "a@example.com", linkedin: true, google: true }], onUsed)
  fireEvent.click(await screen.findByRole("button", { name: /a@example.com/ }))
  await waitFor(() => expect(onUsed).toHaveBeenCalled())
  expect(screen.getByText("Signed in to LinkedIn and Google")).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith("/accounts/api/chrome-profile", expect.objectContaining({ method: "POST" }))
})

it("shows nothing without a profile to pick", async () => {
  const fetchMock = show([])
  await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  expect(screen.queryByRole("button")).toBeNull()
})
