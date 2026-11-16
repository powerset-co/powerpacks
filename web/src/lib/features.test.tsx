import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import { useFeatures, useUpdateFeatures } from "./features"

function Controls() {
  const update = useUpdateFeatures()
  return <button onClick={() => update.mutate({ support: true, team: true })}>Enable</button>
}

function Consumer() {
  const features = useFeatures()
  return (
    <p>
      {features.support ? "Support enabled" : "Support disabled"},{" "}
      {features.team ? "Team enabled" : "Team disabled"}
    </p>
  )
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Controls />
      <Consumer />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it("keeps both features off until local settings load", async () => {
  let finish!: (response: Response) => void
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve
        }),
    ),
  )
  mount()
  expect(screen.getByText("Support disabled, Team disabled")).toBeTruthy()
  finish(new Response(JSON.stringify({ support: true, team: false })))
  expect(await screen.findByText("Support enabled, Team disabled")).toBeTruthy()
})

it("updates other consumers only after settings are saved", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ support: false, team: false })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ support: true, team: true })))
  vi.stubGlobal("fetch", fetch)
  mount()
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByText("Enable"))
  expect(await screen.findByText("Support enabled, Team enabled")).toBeTruthy()
  expect(fetch).toHaveBeenLastCalledWith("/api/features", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ support: true, team: true }),
  })
})

it("keeps experiments off when settings cannot load", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")))
  mount()
  await waitFor(() => expect(screen.getByText("Support disabled, Team disabled")).toBeTruthy())
})
