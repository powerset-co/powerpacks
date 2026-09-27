import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ShareUpload } from "./ShareUpload"

function respond(status: string, uploaded = 0, skipped = 0) {
  return new Response(
    JSON.stringify({
      status,
      progress: { total: 10, uploaded, skipped, namespaces: { people: { upserted: 3, patched: 2 } } },
    }),
    {
      headers: { "Content-Type": "application/json" },
    },
  )
}

function show() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ShareUpload />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ShareUpload", () => {
  it("starts once, shows confirmed counts, and reopens a running upload without posting again", async () => {
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      Promise.resolve(respond(init?.method === "POST" ? "running" : "idle", 3, 2)),
    )
    vi.stubGlobal("fetch", fetch)
    show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByText("Uploading your network")
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("17")
    fireEvent.click(screen.getByRole("button", { name: "Close" }))
    fireEvent.click(screen.getByRole("button", { name: "View upload" }))
    await screen.findByRole("dialog")
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1)
  })

  it("explains an unchanged upload and lets the next click check for changes", async () => {
    const fetch = vi.fn(() => Promise.resolve(respond("completed", 0, 10)))
    vi.stubGlobal("fetch", fetch)
    show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByText("Your network is up to date")
    expect(screen.getByText("Already up to date")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Done" }))
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  })

  it("keeps a failed start recoverable with a retry button", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(respond("completed", 10))
    vi.stubGlobal("fetch", fetch)
    show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByRole("alert")
    fireEvent.click(screen.getByRole("button", { name: "Retry upload" }))
    await screen.findByText("Your network is shared")
  })
})
