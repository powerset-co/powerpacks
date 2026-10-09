import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { LogbookEntry, LogbookMessage } from "@/lib/api/logbook"
import { must } from "@/lib/must"
import {
  DM,
  MESSAGES,
  RESULT,
  THREAD,
  THREAD_PEOPLE,
  monthlyMessages,
  entryDetail,
  logbookStatus,
  savedEntry,
} from "@/testing/logbook-fixture"
import { DETAIL, PAYLOAD } from "@/testing/people-fixture"
import { uploadResponse, uploadStatus } from "@/testing/upload-fixture"

import { LogbookReader } from "./logbook/LogbookReader"
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

/** The saved archive the logbook routes serve; none unless a test saves some. */
let saved: LogbookEntry[] = []
/** The DM's saved messages; a test can swap in a long history. */
let dmMessages: LogbookMessage[] = MESSAGES

/** The saved archive's routes: Casey's entry holds a DM and a Gmail thread. */
function serveSaved(url: string): Response | null {
  if (url.includes("/logbook/entries")) return respond({ entries: saved })
  if (url.includes("/logbook/entry?")) {
    const slug = new URL(url, "http://local").searchParams.get("slug")
    const entry = saved.find((row) => row.slug === slug)
    return entry
      ? respond(entryDetail(entry, [DM, THREAD]))
      : respond({ error: "No saved logbook here." }, 404)
  }
  if (url.includes("/logbook/conversation?")) {
    const path = new URL(url, "http://local").searchParams.get("path")
    return respond(
      path === DM.path
        ? { messages: dmMessages, participants: null }
        : {
            messages: [{ at: "2019-03-01 10:00", sender: "Casey Delta", text: "See you at 7" }],
            participants: THREAD_PEOPLE,
          },
    )
  }
  return null
}

/** The People routes: the payload, one person's detail, a tags write that echoes the post, the upload status. */
function serve(url: string, init?: RequestInit): Promise<Response> {
  const archive = serveSaved(url)
  if (archive) return Promise.resolve(archive)
  if (url.endsWith("/tags")) return Promise.resolve(tagsResponse(init))
  if (url.includes("/person?")) return Promise.resolve(respond(DETAIL))
  if (url.endsWith("/upload")) return Promise.resolve(uploadResponse(uploadStatus()))
  if (url.endsWith("/logbook")) return Promise.resolve(respond(logbookStatus()))
  if (url.includes("/sets")) return Promise.resolve(respond(SETS))
  return Promise.resolve(respond(PAYLOAD))
}

/** The sets route: one personal set with two members; the share count is the page's yes rows. */
const SETS = {
  sets: [
    {
      set_id: "set-1",
      name: "Personal Connections",
      role: "owner",
      is_personal: true,
      member_count: 2,
      person_count: 40,
      members: [
        { name: "Jordan Bravo", email: "jordan@example.com", role: "owner" },
        { name: "Casey Delta", email: "casey@example.com", role: "member" },
      ],
      refreshed_at: "2026-10-08T00:00:00Z",
    },
  ],
  shared: 2,
  default_set_id: "set-1",
}

function respond(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
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

/** Where the router is, for the reader's tests: path and query on a hidden node. */
function Where() {
  const location = useLocation()
  return <output hidden data-at={`${location.pathname}${location.search}`} />
}
const at = () => document.querySelector("[data-at]")?.getAttribute("data-at")

/** The People page and its Logbook reader, routed as App.tsx routes them. */
function renderPage(path = "/people") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/people" element={<PeoplePage />}>
            <Route index element={null} />
            <Route path="logbook" element={<LogbookReader />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  stubLayout()
  sessionStorage.clear()
  saved = []
  dmMessages = MESSAGES
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

  it("shows only the share action beside the tab totals", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await screen.findByRole("button", { name: "Share network" })
    const share = must(container.querySelector<HTMLElement>("[data-head] > .head-share"))
    expect(within(share).getByRole("button", { name: "Share network" })).toBeTruthy()
    expect(container.querySelector("[data-head] .head-note")).toBeNull()
    expect(screen.queryByText("Never shared")).toBeNull()
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
    const bar = must(container.querySelector<HTMLElement>("[data-action-bar]"))
    expect(within(bar).getByText("3 selected")).toBeTruthy()
    fireEvent.click(within(bar).getByRole("button", { name: "Share S" }))
    await waitFor(() => expect(screen.getByText("Marked 3 people for sharing.")).toBeTruthy())
    expect(screen.getByText("No one needs confirmation.")).toBeTruthy()
    expect(
      fetch.mock.calls.filter(
        ([url]) => !url.endsWith("/upload") && !url.includes("/logbook") && !url.includes("/sets"),
      ),
    ).toHaveLength(2)
  })

  it("marks an updating person and disables drawer, bulk and keyboard tag edits", async () => {
    const payload = structuredClone(PAYLOAD)
    const row = must(payload.rows[0])
    row[payload.columns.indexOf("in_progress")] = true
    const fetch = vi.fn((url: string, init?: RequestInit) =>
      url.endsWith("/rows") ? Promise.resolve(respond(payload)) : serve(url, init),
    )
    vi.stubGlobal("fetch", fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const updating = must(container.querySelector<HTMLElement>('.row[data-id="p1"]'))
    expect(within(updating).getByText("Updating")).toBeTruthy()
    fireEvent.click(updating)
    const drawer = must(container.querySelector<HTMLElement>("[data-drawer]"))
    expect(within(drawer).getByText("Jordan Bravo is being updated; finish the run first.")).toBeTruthy()
    expect(within(drawer).getByRole<HTMLButtonElement>("button", { name: "Share" }).disabled).toBe(true)
    expect(within(drawer).getByRole<HTMLButtonElement>("button", { name: "Keep private" }).disabled).toBe(
      true,
    )
    fireEvent.click(must(container.querySelector("[data-select-all]")))
    const bar = must(container.querySelector<HTMLElement>("[data-action-bar]"))
    expect(within(bar).getByRole<HTMLButtonElement>("button", { name: "Share S" }).disabled).toBe(true)
    expect(within(bar).getByRole<HTMLButtonElement>("button", { name: "Keep private P" }).disabled).toBe(true)
    fireEvent.keyDown(document, { key: "s" })
    fireEvent.keyDown(document, { key: "p" })
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/tags"))).toBe(false)
  })

  it("opens the drawer on a row click and closes it on the next", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const drawer = must(container.querySelector("[data-drawer]"))
    const row = must(container.querySelector<HTMLElement>(".row"))
    fireEvent.click(row)
    expect(drawer.getAttribute("data-open")).toBe("true")
    expect(container.querySelector("[data-drawer] h2")?.textContent).toBe("Casey Delta")
    fireEvent.click(row)
    expect(drawer.getAttribute("data-open")).toBe("false")
  })

  it("shows the bar for an open person and moves on to the next one after a label", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const names = () => [...container.querySelectorAll(".row .who b")].map((el) => el.textContent)
    const heading = () => container.querySelector("[data-drawer] h2")?.textContent
    const bar = () => must(container.querySelector<HTMLElement>("[data-action-bar]"))
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
      expect(must(container.querySelector("[data-drawer]")).getAttribute("data-open")).toBe("false"),
    )
  })
})

const DONE_TOAST =
  "Saved 12 messages from 3 conversations. Gmail isn't set up on this computer. " +
  "iMessage couldn't be read."

/** The logbook's routes: the POST answers `started`, every later status read answers `after`. */
function serveLogbook(started: Response, after = logbookStatus()) {
  const posts: unknown[] = []
  let posted = false
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (!url.endsWith("/logbook")) return serve(url, init)
    if (init?.method !== "POST") return Promise.resolve(respond(posted ? after : logbookStatus()))
    posted = true
    posts.push(JSON.parse(typeof init.body === "string" ? init.body : "null"))
    return Promise.resolve(started)
  })
  return { fetch, posts }
}

const reader = () => document.querySelector<HTMLElement>("[data-logbook]")
const list = (container: HTMLElement) => must(container.querySelector<HTMLElement>(".people-page-list"))

describe("PeoplePage logbook", () => {
  it("has no download in the head: the share button follows the count", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    expect(screen.queryByRole("link", { name: /download/i })).toBeNull()
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull()
  })

  it("opens the reader on what a build saved; Back finds the list, selection and scroll as they were", async () => {
    const done = logbookStatus({ status: "completed", people: ["p1", "p2", "p3"], result: RESULT })
    const route = serveLogbook(
      respond(logbookStatus({ status: "building", people: ["p1", "p2", "p3"] })),
      done,
    )
    vi.stubGlobal("fetch", route.fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    // A filtered, re-sorted list, scrolled, with everyone selected.
    fireEvent.change(screen.getByRole("searchbox", { name: "Search people" }), { target: { value: "e" } })
    fireEvent.click(screen.getByRole("button", { name: /^Interactions/ }))
    const viewport = must(container.querySelector<HTMLElement>("[data-viewport]"))
    viewport.scrollTop = 120
    fireEvent.click(must(container.querySelector("[data-select-all]")))
    const bar = must(container.querySelector<HTMLElement>("[data-action-bar]"))
    const order = [...container.querySelectorAll(".row .who b")].map((el) => el.textContent)
    fireEvent.click(within(bar).getByRole("button", { name: "Build logbook" }))

    await waitFor(() => expect(within(bar).getByRole("button", { name: "Building logbook…" })).toBeTruthy())
    expect(route.posts).toHaveLength(1)
    expect(
      screen.getByText(/^Building a logbook for 3 people\. Raw Gmail, iMessage and WhatsApp/),
    ).toBeTruthy()

    // Finished: the reader opens on the built entries, the list hidden and inert beneath it.
    await waitFor(() => expect(at()).toBe("/people/logbook?entry=casey-delta-p2&entry=family"), {
      timeout: 3000,
    })
    expect(screen.getByText(DONE_TOAST)).toBeTruthy()
    expect(reader()).toBeTruthy()
    expect(list(container).inert).toBe(true)
    expect(container.querySelector("[data-reading]")).toBeTruthy()
    // Keys the list would act on do nothing while reading.
    fireEvent.keyDown(document.body, { key: "s" })
    fireEvent.keyDown(document.body, { key: "Escape" })
    await waitFor(() => expect(at()).toBe("/people"))
    expect(route.fetch.mock.calls.filter(([url]) => url.endsWith("/tags"))).toHaveLength(0)

    // Escape went Back: the same nodes, filters, sort, selection and scroll.
    expect(reader()).toBeNull()
    expect(list(container).inert).toBe(false)
    expect(container.querySelector<HTMLElement>("[data-viewport]")).toBe(viewport)
    expect(viewport.scrollTop).toBe(120)
    expect(screen.getByRole<HTMLInputElement>("searchbox", { name: "Search people" }).value).toBe("e")
    expect([...container.querySelectorAll(".row .who b")].map((el) => el.textContent)).toEqual(order)
    expect(within(bar).getByText("3 selected")).toBeTruthy()
  })

  it("views a saved logbook from the drawer and Back returns to the open drawer", async () => {
    saved = [savedEntry()]
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector<HTMLElement>(".row")))
    const drawer = must(container.querySelector<HTMLElement>("[data-drawer]"))
    const view = within(drawer).getByRole("button", { name: "View logbook" })
    view.focus()
    fireEvent.click(view)

    await waitFor(() => expect(at()).toBe("/people/logbook?entry=casey-delta-p2"))
    // The most recent conversation opens; its text renders as text, never as markup.
    const pane = await waitFor(() => must(document.querySelector<HTMLElement>("[data-messages]")))
    await waitFor(() => expect(within(pane).getByText("<b>hi</b> <img src=x onerror=alert(1)>")).toBeTruthy())
    expect(pane.querySelector("img, b:not(.logbook-msg-sender)")).toBeNull()
    expect(within(pane).getByText("second line\nthird line", { normalizer: (text) => text })).toBeTruthy()
    expect(within(pane).getByText("You")).toBeTruthy()
    expect(within(pane).getByText("No text")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "People" }))
    await waitFor(() => expect(at()).toBe("/people"))
    expect(drawer.getAttribute("data-open")).toBe("true")
    expect(container.querySelector("[data-drawer] h2")?.textContent).toBe("Casey Delta")
    expect(document.activeElement).toBe(view)
  })

  it("offers one logbook action: View once saved, Build otherwise, on the bar only for a selection", async () => {
    saved = [savedEntry()]
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const rows = () => [...container.querySelectorAll<HTMLElement>(".row")]
    const row = (name: string) => must(rows().find((each) => each.textContent.includes(name)))
    const checkbox = (name: string) => must(row(name).querySelector<HTMLElement>("input[type=checkbox]"))
    const drawer = must(container.querySelector<HTMLElement>("[data-drawer]"))
    const bar = () => must(container.querySelector<HTMLElement>("[data-action-bar]"))
    const logbookButtons = (scope: HTMLElement) =>
      within(scope)
        .queryAllByRole("button")
        .map((button) => button.textContent)
        .filter((text) => /logbook/i.test(text))

    // The open person: the drawer holds their one action; the bar holds none.
    fireEvent.click(row("Casey Delta"))
    await waitFor(() => expect(within(drawer).getByRole("button", { name: "View logbook" })).toBeTruthy())
    expect(logbookButtons(drawer)).toEqual(["View logbook"])
    expect(logbookButtons(bar())).toEqual([])
    fireEvent.click(row("Riley Echo"))
    await waitFor(() => expect(logbookButtons(drawer)).toEqual(["Build logbook"]))
    fireEvent.click(row("Riley Echo"))

    // A selection: View when everyone has one, else Build, never both.
    fireEvent.click(checkbox("Casey Delta"))
    expect(logbookButtons(bar())).toEqual(["View logbook"])
    fireEvent.click(checkbox("Riley Echo"))
    expect(logbookButtons(bar())).toEqual(["Build logbook"])
    fireEvent.click(checkbox("Riley Echo"))
    fireEvent.click(within(bar()).getByRole("button", { name: "View logbook" }))
    await waitFor(() => expect(at()).toBe("/people/logbook?entry=casey-delta-p2"))
  })

  it("refreshes the person being read and keeps the reader's scope and Back", async () => {
    saved = [savedEntry()]
    const done = logbookStatus({ status: "completed", people: ["p2"], result: RESULT })
    const route = serveLogbook(respond(done), done)
    vi.stubGlobal("fetch", route.fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector<HTMLElement>(".row")))
    fireEvent.click(await screen.findByRole("button", { name: "View logbook" }))
    await waitFor(() => expect(at()).toBe("/people/logbook?entry=casey-delta-p2"))

    fireEvent.click(await screen.findByRole("button", { name: "Refresh logbook" }))
    await waitFor(() => expect(at()).toBe("/people/logbook?entry=casey-delta-p2&entry=family"))
    expect(route.posts).toEqual([{ people: ["p2"] }])
    // The refreshed scope replaced the reader's entry: Back still leads to People.
    fireEvent.click(screen.getByRole("button", { name: "People" }))
    await waitFor(() => expect(at()).toBe("/people"))
  })

  it("still lists People when the saved logbooks can't be read, and says so instead of claiming none", async () => {
    saved = [savedEntry()]
    let failing = true
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) =>
        failing && url.includes("/logbook/entries")
          ? Promise.resolve(respond({ error: "boom" }, 500))
          : serve(url, init),
      ),
    )
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const notice = await waitFor(() => must(container.querySelector<HTMLElement>("[data-logbook-unread]")))
    expect(notice.textContent).toContain("Couldn't read saved logbooks.")
    const rail = must(container.querySelector<HTMLElement>("[data-rail]"))
    expect(rail.querySelector("[data-facet='logbook']")).toBeNull()
    expect(within(rail).queryByText("No logbook")).toBeNull()

    failing = false
    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }))
    await waitFor(() => expect(within(rail).getByRole("button", { name: /Has logbook/ })).toBeTruthy())
    expect(container.querySelector("[data-logbook-unread]")).toBeNull()
  })

  it("filters People to those with a saved logbook", async () => {
    saved = [savedEntry()]
    vi.stubGlobal("fetch", vi.fn(serve))
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    const rail = must(container.querySelector<HTMLElement>("[data-rail]"))
    fireEvent.click(await within(rail).findByRole("button", { name: /Has logbook/ }))
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(1))
    expect(container.querySelector(".row .who b")?.textContent).toBe("Casey Delta")
  })

  it("reads a saved logbook on a fresh load and shows exactly the conversation picked", async () => {
    saved = [
      savedEntry({ channels: ["gmail", "whatsapp"] }),
      savedEntry({ slug: "family", name: "Family", kind: "group", parent_id: null }),
    ]
    const fetch = vi.fn(serve)
    vi.stubGlobal("fetch", fetch)
    renderPage("/people/logbook?entry=casey-delta-p2")

    const nav = await screen.findByRole("navigation", { name: "Saved logbooks" })
    const dm = await within(nav).findByRole("button", { name: /WhatsApp direct messages/ })
    await waitFor(() => expect(dm.getAttribute("aria-current")).toBe("true"))
    const pane = () => must(document.querySelector<HTMLElement>("[data-conversation]"))
    // Whose logbook, which channel's conversation, and its body.
    expect(within(pane()).getByRole("heading", { name: "WhatsApp direct messages" })).toBeTruthy()
    expect(within(pane()).getByText("Casey Delta", { selector: ".logbook-eyebrow-name" })).toBeTruthy()
    expect(within(pane()).getByRole("img", { name: "WhatsApp" })).toBeTruthy()
    await within(pane()).findByText("<b>hi</b> <img src=x onerror=alert(1)>")
    // A direct message names no Senders line: its title already says who.
    expect(pane().querySelector("[data-participants]")).toBeNull()
    expect(within(nav).queryByText("Family")).toBeNull()

    // The thread: its title, Gmail, the mail store's people by role, and no trace of the DM.
    fireEvent.click(within(nav).getByRole("button", { name: /Dinner plans/ }))
    expect(within(pane()).getByRole("heading", { name: "Dinner plans" })).toBeTruthy()
    expect(within(pane()).getByRole("img", { name: "Gmail" })).toBeTruthy()
    expect(within(pane()).queryByText("<b>hi</b> <img src=x onerror=alert(1)>")).toBeNull()
    await within(pane()).findByText("See you at 7")
    const people = must(pane().querySelector<HTMLElement>("[data-participants]"))
    expect([...people.querySelectorAll("div")].map((line) => line.textContent)).toEqual([
      "FromCasey Delta <casey@example.com>",
      "ToCasey Delta <casey@example.com>, Jordan Bravo <jordan@example.com>",
      "CcRiley Echo <riley@example.com>",
    ])
    expect(
      within(nav)
        .getByRole("button", { name: /Dinner plans/ })
        .getAttribute("aria-current"),
    ).toBe("true")
    expect(dm.getAttribute("aria-current")).toBeNull()
    expect(new URLSearchParams((at() ?? "").split("?")[1]).get("conversation")).toBe(THREAD.path)
    // No build ran and nothing was posted to read it.
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0)

    // The rail's filter: by name, and by channel.
    const filter = screen.getByRole("searchbox", { name: "Filter conversations" })
    fireEvent.change(filter, { target: { value: "dinner" } })
    expect(within(nav).queryByRole("button", { name: /WhatsApp direct messages/ })).toBeNull()
    expect(within(nav).getByText("1 of 2 conversations")).toBeTruthy()
    fireEvent.change(filter, { target: { value: "" } })
    fireEvent.click(
      within(screen.getByRole("group", { name: "Channels" })).getByRole("button", { name: "WhatsApp" }),
    )
    expect(within(nav).queryByRole("button", { name: /Dinner plans/ })).toBeNull()
    expect(within(nav).getByRole("button", { name: /WhatsApp direct messages/ })).toBeTruthy()

    // All logbooks starts fresh on the wider scope: the group shows.
    fireEvent.click(screen.getByRole("link", { name: "All logbooks" }))
    await waitFor(() => expect(at()).toBe("/people/logbook"))
    expect(
      await within(await screen.findByRole("navigation", { name: "Saved logbooks" })).findByText("Family"),
    ).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "People" }))
    await waitFor(() => expect(at()).toBe("/people"))
  })

  it("jumps by month on the timeline and marks the month scrolled into view", async () => {
    // Rows measure 40px; the viewport really scrolls when the virtualizer asks it to.
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get: () => 40 })
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => 100_000 })
    Element.prototype.scrollTo = function (this: Element, options?: ScrollToOptions | number) {
      if (typeof options !== "object") return
      this.scrollTop = options.top ?? this.scrollTop
      this.dispatchEvent(new Event("scroll"))
    }
    try {
      saved = [savedEntry()]
      dmMessages = monthlyMessages(2023, 24)
      vi.stubGlobal("fetch", vi.fn(serve))
      renderPage("/people/logbook?entry=casey-delta-p2")
      const timeline = await screen.findByRole("navigation", { name: "Timeline" })
      const viewport = must(document.querySelector<HTMLElement>("[data-messages]"))
      const month = (name: string) => within(timeline).getByRole("button", { name: new RegExp(`^${name},`) })
      // The day line at the top of the view: the mounted row whose offset is the scroll position.
      const topDay = () =>
        [...viewport.querySelectorAll<HTMLElement>("[data-index]")].find(
          (row) => row.style.transform === `translateY(${viewport.scrollTop}px)`,
        )?.textContent
      expect(
        within(timeline)
          .getAllByRole("heading")
          .map((year) => year.textContent),
      ).toEqual(["2023", "2024"])
      expect(within(timeline).getAllByRole("button", { name: /messages$/ })).toHaveLength(24)
      expect(month("Jan 2023").getAttribute("aria-current")).toBe("true")

      fireEvent.click(month("Mar 2024"))
      await waitFor(() => expect(topDay()).toBe("Fri, Mar 1, 2024"))
      expect(month("Mar 2024").getAttribute("aria-current")).toBe("true")
      expect(within(viewport).getByText("Hello 2024-03")).toBeTruthy()
      const march = viewport.scrollTop

      fireEvent.change(within(timeline).getByRole("combobox", { name: "Jump to month" }), {
        target: { value: "2023-06" },
      })
      await waitFor(() => expect(topDay()).toBe("Thu, Jun 1, 2023"))
      expect(month("Jun 2023").getAttribute("aria-current")).toBe("true")

      fireEvent.click(within(timeline).getByRole("button", { name: "Oldest" }))
      await waitFor(() => expect(viewport.scrollTop).toBe(0))
      await waitFor(() => expect(month("Jan 2023").getAttribute("aria-current")).toBe("true"))

      // Scrolling by hand back into March marks March.
      viewport.scrollTop = march + 20
      fireEvent.scroll(viewport)
      await waitFor(() => expect(month("Mar 2024").getAttribute("aria-current")).toBe("true"))
    } finally {
      Reflect.deleteProperty(HTMLElement.prototype, "scrollHeight")
    }
  })

  it("restores the selected email from its URL", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    saved = [savedEntry({ channels: ["gmail", "whatsapp"] })]
    renderPage(`/people/logbook?entry=casey-delta-p2&conversation=${encodeURIComponent(THREAD.path)}`)
    expect(await screen.findByText("See you at 7")).toBeTruthy()
    expect(document.querySelector("[data-conversation]")?.getAttribute("data-conversation")).toBe(THREAD.path)
    expect(screen.queryByText("<b>hi</b> <img src=x onerror=alert(1)>")).toBeNull()
  })

  it("says when the named logbooks are not saved", async () => {
    vi.stubGlobal("fetch", vi.fn(serve))
    renderPage("/people/logbook?entry=gone-1234")
    expect(await screen.findByText("These logbooks aren't saved on this computer.")).toBeTruthy()
    fireEvent.click(screen.getByRole("link", { name: "All logbooks" }))
    expect(await screen.findByText("No saved logbooks yet. Build one from People.")).toBeTruthy()
  })

  it("builds the open person's logbook from the drawer and keeps it open", async () => {
    const done = logbookStatus({ status: "completed", people: ["p2"], result: RESULT })
    const route = serveLogbook(respond(done), done)
    vi.stubGlobal("fetch", route.fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector<HTMLElement>(".row")))
    const drawer = must(container.querySelector<HTMLElement>("[data-drawer]"))
    fireEvent.click(within(drawer).getByRole("button", { name: "Build logbook" }))

    await waitFor(() => expect(screen.getByText(DONE_TOAST)).toBeTruthy())
    expect(route.posts).toEqual([{ people: ["p2"] }])
    await waitFor(() => expect(at()).toBe("/people/logbook?entry=casey-delta-p2&entry=family"))
    expect(drawer.getAttribute("data-open")).toBe("true")
    expect(container.querySelector("[data-drawer] h2")?.textContent).toBe("Casey Delta")
  })

  it("stays on People when a build saved nothing", async () => {
    const empty = { ...RESULT, entries: [], messages: 0, files: 0 }
    const done = logbookStatus({ status: "completed", people: ["p2"], result: empty })
    vi.stubGlobal("fetch", serveLogbook(respond(done), done).fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector<HTMLElement>(".row")))
    fireEvent.click(
      within(must(container.querySelector<HTMLElement>("[data-drawer]"))).getByRole("button", {
        name: "Build logbook",
      }),
    )
    await waitFor(() => expect(screen.getByText(/^No messages found on this computer\./)).toBeTruthy())
    expect(at()).toBe("/people")
  })

  it("says why a build was refused and keeps the selection", async () => {
    const refused = new Response(JSON.stringify({ error: "A logbook is already being built." }), {
      status: 409,
    })
    vi.stubGlobal("fetch", serveLogbook(refused).fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector("[data-select-all]")))
    const bar = must(container.querySelector<HTMLElement>("[data-action-bar]"))
    fireEvent.click(within(bar).getByRole("button", { name: "Build logbook" }))

    await waitFor(() =>
      expect(screen.getByText("Couldn't build the logbook. A logbook is already being built.")).toBeTruthy(),
    )
    expect(within(bar).getByText("3 selected")).toBeTruthy()
    expect(within(bar).getByRole("button", { name: "Build logbook" }).hasAttribute("disabled")).toBe(false)
  })

  it("reports a build that failed while it ran", async () => {
    const failed = logbookStatus({ status: "failed", people: ["p2"], error: "disk full" })
    vi.stubGlobal("fetch", serveLogbook(respond(failed), failed).fetch)
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelectorAll(".row")).toHaveLength(3))
    fireEvent.click(must(container.querySelector<HTMLElement>(".row")))
    const drawer = must(container.querySelector<HTMLElement>("[data-drawer]"))
    fireEvent.click(within(drawer).getByRole("button", { name: "Build logbook" }))

    await waitFor(() => expect(screen.getByText("Couldn't build the logbook. disk full")).toBeTruthy())
    expect(drawer.getAttribute("data-open")).toBe("true")
    expect(at()).toBe("/people")
  })
})
