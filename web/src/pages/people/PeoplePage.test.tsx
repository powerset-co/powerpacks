import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { DETAIL, PAYLOAD } from "@/testing/people-fixture"

import { PeoplePage } from "./PeoplePage"

// jsdom has no layout, Web Animations, matchMedia or ResizeObserver; the virtualizer reads
// offset sizes. Reduced motion, as in the browser tests: overlays mount and unmount at once.
function stubLayout() {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 600 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 1200 })
  HTMLElement.prototype.animate = vi.fn()
  // The virtualizer scrolls the viewport to the row the drawer moves to.
  Element.prototype.scrollTo = vi.fn()
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

/** The People routes: the payload, one person's detail, and a tags write that echoes the post. */
function serve(url: string, init?: RequestInit): Promise<Response> {
  if (url.endsWith("/tags")) return Promise.resolve(tagsResponse(init))
  if (url.includes("/person?")) return Promise.resolve(respond(DETAIL))
  return Promise.resolve(respond(PAYLOAD))
}

function respond(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
}

/** A tags response echoing what was posted: share when the tags say so, else not sharing. */
function tagsResponse(init?: RequestInit): Response {
  const rows = sentPeople(init).map((entry) => {
    const person = must(isTagged(entry) ? entry : null, "a tagged person")
    return {
      parent_id: person.parent_id,
      share: person.tags.includes("share") ? "yes" : "no",
      reason: person.tags.includes("share") ? "human_share" : "human_private",
      share_source: "human",
      tags: person.tags,
    }
  })
  return respond({ rows })
}

function isTagged(value: unknown): value is { parent_id: string; tags: string[] } {
  return (
    typeof value === "object" &&
    value !== null &&
    "parent_id" in value &&
    typeof value.parent_id === "string" &&
    "tags" in value &&
    Array.isArray(value.tags)
  )
}

/** The `people` array a tags request carried. */
function sentPeople(init?: RequestInit): unknown[] {
  const sent: unknown = JSON.parse(typeof init?.body === "string" ? init.body : "null")
  if (!sent || typeof sent !== "object" || !("people" in sent) || !Array.isArray(sent.people)) {
    throw new Error("the tags request carried no people")
  }
  return sent.people
}

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
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    expect(container.querySelector("[data-tab='confirm']")?.getAttribute("aria-pressed")).toBe("true")
    expect(screen.getByText("3 people", { selector: "[data-count]" })).toBeTruthy()
  })

  it("selects every matching person and writes share for them, then offers undo", async () => {
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/tags")) expect(sentPeople(init)[2]).toEqual({ parent_id: "p3", tags: ["share"] })
      return serve(url, init)
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
    vi.stubGlobal("fetch", vi.fn(serve))
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

  it("shows the bar for an open person and moves on to the next one after a label", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const names = () => [...container.querySelectorAll(".row .who b")].map((el) => el.textContent)
    const heading = () => container.querySelector("[data-drawer] h2")?.textContent
    const bar = () => must(container.querySelector<HTMLElement>("[data-bulkbar]"))
    const share = () => fireEvent.click(within(bar()).getByRole("button", { name: "Share S" }))
    const [first, second, third] = names()
    // Open the middle person: the bar names them, and Use worth is gone.
    fireEvent.click(must(container.querySelectorAll(".row")[1]))
    expect(within(bar()).getByText(second ?? "")).toBeTruthy()
    expect(within(bar()).queryByText(/Use worth/)).toBeNull()
    // Share moves them out of the tab; the drawer opens whoever took their place.
    share()
    await waitFor(() => expect(names()).toEqual([first, third]))
    await waitFor(() => expect(heading()).toBe(third))
    // At the end of the list it walks backwards.
    share()
    await waitFor(() => expect(names()).toEqual([first]))
    await waitFor(() => expect(heading()).toBe(first))
    // The last one: nobody is left, so the drawer closes.
    share()
    await waitFor(() => expect(names()).toEqual([]))
    await waitFor(() =>
      expect(must(container.querySelector("[data-people]")).getAttribute("data-drawer-open")).toBe("false"),
    )
  })
})
