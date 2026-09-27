import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ShareUpload } from "./ShareUpload"

function respond(status: string, uploaded = 0, skipped = 0) {
  return new Response(
    JSON.stringify({
      status,
      previously_uploaded: 8,
      progress: { total: 10, uploaded, skipped },
    }),
    { headers: { "Content-Type": "application/json" } },
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
  it("keeps saved counts in the modal and only uploads after confirmation", async () => {
    const fetch = vi.fn((url: string, _init?: RequestInit) =>
      Promise.resolve(respond(url.endsWith("/check") ? "ready" : "completed", 2, 8)),
    )
    vi.stubGlobal("fetch", fetch)
    show()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/Last upload/)).toBeNull()
    expect(screen.queryByText("Previously uploaded")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByRole("heading", { name: "Ready to share" })
    expect(screen.getByText("Previously uploaded")).toBeTruthy()
    expect(screen.getByText("8")).toBeTruthy()
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => url)).toEqual([
      "/api/people/upload/check",
    ])
    fireEvent.click(screen.getByRole("button", { name: "Confirm sharing" }))
    await screen.findByText("Your network is shared")
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => url)).toEqual([
      "/api/people/upload/check",
      "/api/people/upload",
    ])
  })

  it("closing a checked modal never uploads, including after refresh", async () => {
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      Promise.resolve(respond(init?.method === "POST" ? "ready" : "completed")),
    )
    vi.stubGlobal("fetch", fetch)
    const first = show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByText("Ready to share")
    fireEvent.click(screen.getByText("Close", { selector: "button" }))
    first.unmount()
    show()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => url)).toEqual([
      "/api/people/upload/check",
    ])
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("reopens an active upload without another POST", async () => {
    const fetch = vi.fn(() => Promise.resolve(respond("running")))
    vi.stubGlobal("fetch", fetch)
    show()
    fireEvent.click(await screen.findByRole("button", { name: "View upload" }))
    await screen.findByRole("dialog")
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole("button", { name: "Confirm sharing" })).toBeNull()
  })

  it("shows a spinner while checking and disables confirmation", async () => {
    let finish!: (response: Response) => void
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) =>
        init?.method === "POST"
          ? new Promise<Response>((resolve) => {
              finish = resolve
            })
          : Promise.resolve(respond("completed")),
      ),
    )
    show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    expect(screen.getByRole("status", { name: "Upload in progress" })).toBeTruthy()
    expect(screen.getByRole("heading", { name: "Checking your network" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Confirm sharing" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByRole("progressbar").hasAttribute("aria-valuenow")).toBe(false)
    await waitFor(() => expect(finish).toBeDefined())
    finish(respond("ready"))
    await screen.findByText("Ready to share")
  })

  it("shows a failed confirmation and checks again before retrying", async () => {
    const fetch = vi
      .fn<(url: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(respond("completed"))
      .mockResolvedValueOnce(respond("ready"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(respond("ready"))
    vi.stubGlobal("fetch", fetch)
    show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByText("Ready to share")
    fireEvent.click(screen.getByRole("button", { name: "Confirm sharing" }))
    await screen.findByRole("alert")
    fireEvent.click(screen.getByRole("button", { name: "Retry check" }))
    await screen.findByText("Ready to share")
    expect(fetch.mock.calls.slice(1).map(([url]) => url)).toEqual([
      "/api/people/upload/check",
      "/api/people/upload",
      "/api/people/upload/check",
    ])
  })

  it("retries a failed check without applying", async () => {
    const fetch = vi
      .fn<(url: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(respond("idle"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(respond("ready"))
    vi.stubGlobal("fetch", fetch)
    show()
    fireEvent.click(screen.getByRole("button", { name: "Share network" }))
    await screen.findByRole("alert")
    fireEvent.click(screen.getByRole("button", { name: "Retry check" }))
    await screen.findByText("Ready to share")
    expect(fetch.mock.calls.slice(1).map(([url]) => url)).toEqual([
      "/api/people/upload/check",
      "/api/people/upload/check",
    ])
  })
})
