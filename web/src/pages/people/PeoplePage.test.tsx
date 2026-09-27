import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { PAYLOAD } from "@/testing/people-fixture"

import { PeoplePage } from "./PeoplePage"

// jsdom has no layout, Web Animations, matchMedia or ResizeObserver; the virtualizer reads
// offset sizes. Reduced motion, as in the browser tests: overlays mount and unmount at once.
function stubLayout() {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 1200 })
  HTMLElement.prototype.animate = vi.fn()
  HTMLElement.prototype.getAnimations = () => []
  vi.stubGlobal("matchMedia", (media: string) => ({
    matches: true,
    media,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {
        // Layout is stubbed; nothing to observe.
      }
      unobserve() {
        // See observe.
      }
      disconnect() {
        // See observe.
      }
    },
  )
}

function respond(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
}

/** The `people` array a tags request carried. */
function sentPeople(init?: RequestInit): unknown[] {
  const sent: unknown = JSON.parse(typeof init?.body === "string" ? init.body : "null")
  if (!sent || typeof sent !== "object" || !("people" in sent) || !Array.isArray(sent.people)) {
    throw new Error("the tags request carried no people")
  }
  return sent.people
}

// The fixture's people on the confirm tab, in row order.
const CONFIRM_IDS = PAYLOAD.rows
  .filter((values) => values[PAYLOAD.columns.indexOf("share")] === "confirm")
  .map((values) => String(values[PAYLOAD.columns.indexOf("parent_id")]))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PeoplePage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  stubLayout()
  sessionStorage.clear()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("PeoplePage", () => {
  it("opens on the confirm tab with the tab totals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(respond(PAYLOAD))),
    )
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    expect(container.querySelector("[data-tab='confirm']")?.getAttribute("aria-pressed")).toBe("true")
    expect(screen.getByText("3 people", { selector: "[data-count]" })).toBeTruthy()
  })

  it("selects every matching person and writes share for them, then offers undo", async () => {
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (!url.endsWith("/tags")) return Promise.resolve(respond(PAYLOAD))
      const rows = CONFIRM_IDS.map((parent_id) => ({
        parent_id,
        share: "yes",
        reason: "human_share",
        share_source: "human",
        tags: ["share"],
      }))
      expect(sentPeople(init)[2]).toEqual({ parent_id: "p3", tags: ["share"] })
      return Promise.resolve(respond({ rows }))
    })
    vi.stubGlobal("fetch", fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector("[data-select-all]")))
    const bar = must(container.querySelector<HTMLElement>("[data-bulkbar]"))
    expect(within(bar).getByText("3 selected")).toBeTruthy()
    fireEvent.click(within(bar).getByRole("button", { name: "Share S" }))
    await waitFor(() => expect(screen.getByText("Marked 3 people for sharing.")).toBeTruthy())
    expect(screen.getByText("No one needs confirmation.")).toBeTruthy()
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it("opens the drawer on a row click and closes it on the next", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(respond(PAYLOAD))),
    )
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const shell = must(container.querySelector("[data-people]"))
    const row = must(container.querySelector<HTMLElement>(".row"))
    fireEvent.click(row)
    expect(shell.getAttribute("data-drawer-open")).toBe("true")
    expect(container.querySelector("[data-drawer] h2")?.textContent).toBe("Casey Delta")
    fireEvent.click(row)
    expect(shell.getAttribute("data-drawer-open")).toBe("false")
  })
})
