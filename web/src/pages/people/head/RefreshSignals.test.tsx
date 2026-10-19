import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { SignalsPlan, SignalsStatus } from "@/lib/api/signals"

import { RefreshSignals } from "./RefreshSignals"

const PLAN: SignalsPlan = {
  people: 2,
  cached: 1,
  new_requests: 1,
  estimated_cost_usd: 0.0042,
  estimated_seconds: 1,
  checked: "exact",
}
const IDLE: SignalsStatus = {
  status: "idle",
  message: null,
  plan: null,
  error: null,
  done: 0,
  total: 0,
  refused: 0,
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function show(plan = PLAN, people: readonly string[] = ["p:a", "p:b"], refused = 0) {
  let status = IDLE
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST" && !url.endsWith("/check")) {
      status = { ...IDLE, status: "completed", plan, refused }
    }
    if (url.endsWith("/check")) {
      status = { ...IDLE, plan }
      return Promise.resolve(
        new Response(JSON.stringify({ ...IDLE, status: "running", message: "Reading families" })),
      )
    }
    return Promise.resolve(new Response(JSON.stringify(status)))
  })
  vi.stubGlobal("fetch", fetch)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(client, "invalidateQueries")
  const done = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <RefreshSignals people={people} onDone={done} />
    </QueryClientProvider>,
  )
  return { fetch, invalidate, done }
}

describe("Refresh signals", () => {
  it("opening People only reads status and paid refresh waits for exact cost confirmation", async () => {
    const { fetch, invalidate, done } = show()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(fetch.mock.calls[0]?.[1]).toBeUndefined()
    // The header button says which people it refreshes: here, the two selected.
    fireEvent.click(screen.getByRole("button", { name: "Refresh signals 2 selected" }))
    await screen.findByText("2 people · under a minute", {}, { timeout: 2500 })
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1)
    const pay = screen.getByRole("button", { name: "Refresh for <$0.01" })
    expect(pay.title).toBe("Estimated $0.0042")
    fireEvent.click(pay)
    await screen.findByText("Signals refreshed for 2 people.", {}, { timeout: 2500 })
    expect(fetch.mock.calls.at(-1)?.[1]?.body).toBe(
      JSON.stringify({ people: ["p:a", "p:b"], checked: "exact" }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["people"] })
    expect(done).toHaveBeenCalledOnce()
  })

  it("with no one selected, whoever needs it refreshes from saved answers without a cost confirmation", async () => {
    const { fetch } = show({ ...PLAN, cached: 2, new_requests: 0, estimated_cost_usd: 0 }, [])
    fireEvent.click(screen.getByRole("button", { name: "Refresh signals All" }))
    await screen.findByText("Signals refreshed for 2 people.", {}, { timeout: 2500 })
    expect(screen.queryByRole("button", { name: /Refresh for/ })).toBeNull()
    expect(fetch.mock.calls.at(-1)?.[1]?.body).toBe(JSON.stringify({ people: [] }))
  })

  it("says how many Luna declined once the refresh is done", async () => {
    show({ ...PLAN, cached: 2, new_requests: 0, estimated_cost_usd: 0 }, [], 1)
    fireEvent.click(screen.getByRole("button", { name: /^Refresh signals/ }))
    expect(
      await screen.findByText(
        "Signals refreshed for 1 person. Luna declined 1 person; they keep their old signals.",
        {},
        { timeout: 2500 },
      ),
    ).toBeTruthy()
  })

  it("says signals are up to date and starts nothing when no one needs them", async () => {
    const { fetch } = show({ ...PLAN, people: 0, cached: 0, new_requests: 0, estimated_cost_usd: 0 }, [])
    fireEvent.click(screen.getByRole("button", { name: /^Refresh signals/ }))
    await screen.findByText("Signals are up to date.", {}, { timeout: 2500 })
    expect(
      fetch.mock.calls.filter(([url, init]) => url.endsWith("/signals") && init?.method === "POST"),
    ).toHaveLength(0)
  })

  it("follows a running refresh with a progress bar", async () => {
    const running: SignalsStatus = {
      ...IDLE,
      status: "running",
      message: "Reading signals",
      plan: PLAN,
      done: 3,
      total: 8,
    }
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify(running)))),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <RefreshSignals people={[]} onDone={vi.fn()} />
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole("button", { name: /Refreshing signals/ }))
    expect((await screen.findByRole("progressbar")).getAttribute("aria-valuenow")).toBe("3")
    expect(screen.getByText("3 of 8 requests")).toBeTruthy()
  })

  it("failed check shows the error and retries the estimate without starting paid work", async () => {
    const { fetch } = show()
    fetch.mockImplementation((url) =>
      Promise.resolve(
        url.endsWith("/check")
          ? new Response(JSON.stringify({ error: "Saved evidence changed" }), { status: 409 })
          : new Response(JSON.stringify(IDLE)),
      ),
    )
    fireEvent.click(screen.getByRole("button", { name: /^Refresh signals/ }))
    expect((await screen.findByRole("alert")).textContent).toBe("Saved evidence changed")
    fireEvent.click(screen.getByRole("button", { name: "Check again" }))
    await waitFor(() => expect(fetch.mock.calls.filter(([url]) => url.endsWith("/check"))).toHaveLength(2))
    expect(
      fetch.mock.calls.filter(([url, init]) => url.endsWith("/signals") && init?.method === "POST"),
    ).toHaveLength(0)
  })
})

it("shows family progress while checking, then the paid plan without refreshing", async () => {
  let status = IDLE
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/check") && init?.method === "POST")
      status = { ...IDLE, status: "running", message: "Reading families", done: 3200, total: 6346 }
    return Promise.resolve(new Response(JSON.stringify(status)))
  })
  vi.stubGlobal("fetch", fetch)
  const done = vi.fn()
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RefreshSignals people={[]} onDone={done} />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole("button", { name: /^Refresh signals/ }))
  expect(await screen.findByText("Reading families")).toBeTruthy()
  expect(screen.getByText("3,200 of 6,346")).toBeTruthy()
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("3200")
  status = { ...IDLE, plan: PLAN }
  expect(await screen.findByRole("button", { name: "Refresh for <$0.01" }, { timeout: 2500 })).toBeTruthy()
  expect(done).not.toHaveBeenCalled()
  expect(
    fetch.mock.calls.filter(([url, init]) => url.endsWith("/signals") && init?.method === "POST"),
  ).toHaveLength(0)
})

it("does not refresh from a previous selection's completed check on mount", async () => {
  const fetch = vi.fn(() =>
    Promise.resolve(new Response(JSON.stringify({ ...IDLE, plan: { ...PLAN, new_requests: 0 } }))),
  )
  vi.stubGlobal("fetch", fetch)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RefreshSignals people={[]} onDone={vi.fn()} />
    </QueryClientProvider>,
  )
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole("button", { name: /Refresh for/ })).toBeNull()
})

it("reports a failed background check without announcing a completed refresh", async () => {
  let status = IDLE
  const done = vi.fn()
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url.endsWith("/check")) {
        status = { ...IDLE, status: "failed", error: "Could not read saved messages" }
        return Promise.resolve(
          new Response(JSON.stringify({ ...IDLE, status: "running", message: "Reading families" })),
        )
      }
      return Promise.resolve(new Response(JSON.stringify(status)))
    }),
  )
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RefreshSignals people={[]} onDone={done} />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole("button", { name: /^Refresh signals/ }))
  expect((await screen.findByRole("alert", {}, { timeout: 2500 })).textContent).toBe(
    "Could not read saved messages",
  )
  expect(screen.getByRole("button", { name: "Check again" }).hasAttribute("disabled")).toBe(false)
  expect(done).not.toHaveBeenCalled()
})
