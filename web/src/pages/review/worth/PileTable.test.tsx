import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { wait } from "@/lib/review/timing"
import {
  errorResponse,
  jsonResponse,
  motionMedia,
  pageProgress,
  worthDetails,
} from "@/testing/review-fixture"
import type { DecisionRow, WorthTab } from "@/types/review"

import type { Review } from "../hooks/useReview"
import { OPEN_ROW_PX, ROW_PX, stubListLayout, type ListLayout } from "./list-layout"
import {
  gate,
  personNamed,
  refusal,
  renderWorth,
  rowNamed,
  tabText,
  worthServer,
  type WorthServer,
} from "./worth-fixture"

const TABLE = "/api/review/worth-table"
const DETAILS = "/api/review/worth-details"
const DOSSIER = "/api/dossier"
/** What the page loaded with; the server's own counts differ, so a corrected count shows. */
const PAGE = pageProgress({ worth_pending: 4, worth_yes: 9, worth_no: 7 })

let server: WorthServer
let layout: ListLayout

/** `count` people in name order: "P000 Sample", "P001 Sample", … */
function pileOf(count: number): DecisionRow[] {
  return Array.from({ length: count }, (_, position) =>
    rowNamed(`P${String(position).padStart(3, "0")} Sample`),
  )
}
const named = (position: number) => `P${String(position).padStart(3, "0")} Sample`

/** Five people on Yes and two on No: each pile is one page and fits the list. */
function smallPiles() {
  return worthServer({
    pending: [],
    yes: ["Avery Fox", "Blake Gray", "Casey Delta", "Jordan Bravo", "Riley Echo"].map((name) =>
      rowNamed(name),
    ),
    no: [
      rowNamed("Morgan Hale", { reason: "Not worth adding" }),
      rowNamed("Quinn Ives", { reason: "You said no" }),
    ],
  })
}

function serve(next: WorthServer) {
  server = next
  vi.stubGlobal("fetch", server.fetch)
}

beforeEach(() => {
  layout = stubListLayout()
  serve(smallPiles())
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const list = () => must(document.querySelector<HTMLElement>(".decision-list"), "the list")
const table = () => must(list().querySelector<HTMLElement>(".decision-table"), "the table")
const total = () => table().getAttribute("data-total")
const spacers = () =>
  [...table().querySelectorAll<HTMLElement>(":scope > .virtual-spacer")].map((spacer) => spacer.style.height)
const loading = () => must(list().querySelector<HTMLElement>(":scope > .decision-loading"))
const rows = () => [...document.querySelectorAll<HTMLDetailsElement>("details.decision-row")]
const rowNames = () => rows().map((row) => row.querySelector("summary strong")?.textContent)
const findRow = (name: string) =>
  rows().find((candidate) => candidate.querySelector("summary strong")?.textContent === name)
const row = (name: string) => must(findRow(name), `${name}'s row`)
const flipButton = (name: string) => must(row(name).querySelector<HTMLButtonElement>("summary button"))
const detail = (name: string) => must(row(name).querySelector<HTMLElement>(".decision-row-detail"))
const details = (name: string) => must(detail(name).querySelector<HTMLElement>(":scope > .dossier-text"))
const facts = (within: Element, list: string) =>
  [...within.querySelectorAll(`${list} > div`)].map((fact) => [
    fact.querySelector("dt")?.textContent,
    fact.querySelector("dd")?.textContent,
  ])
/** Long enough for the frame after a change (the list's next look at its end) to have run. */
const settle = () => act(() => wait(50))

/** A decided pile's list with its first page on screen. */
async function openPile(pile: Exclude<WorthTab, "review"> = "yes", overrides: Partial<Review> = {}) {
  const view = renderWorth(pile, server, { progress: PAGE, ...overrides })
  await waitFor(() => expect(document.querySelector(".decision-list")).toBeTruthy())
  await settle()
  return view
}

/** Opens (or closes) a row the way a click on its summary does. */
async function toggle(name: string) {
  const open = !row(name).open
  fireEvent.click(must(row(name).querySelector("summary")))
  await waitFor(() => expect(row(name).open).toBe(open))
  // jsdom fires `toggle` a task later.
  await act(() => wait(0))
}

/** Holds the next page read until the gate opens. */
function holdPage() {
  const held = gate()
  server.table.mockImplementationOnce(async (query) => {
    await held.opened
    return server.readTable(query)
  })
  return held
}

// Point 1
describe("PileTable: the list", () => {
  it.each([
    ["yes", "Yes decisions"],
    ["no", "No decisions"],
  ] as const)("draws the %s pile as a scrolling list a keyboard can reach", async (pile, label) => {
    const { container } = await openPile(pile)
    const box = must(container.querySelector<HTMLElement>(".worth-panel > .decision-list"))
    expect(box.tabIndex).toBe(0)
    expect(box.getAttribute("aria-label")).toBe(label)
    expect([...box.children].map((child) => child.className)).toEqual(["decision-table", "decision-loading"])
    expect(table().getAttribute("data-view")).toBe(pile)
    expect(total()).toBe(String(server.piles[pile].length))
    expect(loading().tagName).toBe("P")
    expect(loading().getAttribute("role")).toBe("status")
    expect(loading().textContent).toBe("Loading…")
    expect(loading().hidden).toBe(true)
    expect(server.reads(TABLE)).toEqual([`${TABLE}?view=${pile}&offset=0`])
  })

  it("has no Show more button", async () => {
    serve(worthServer({ pending: [], yes: pileOf(250) }))
    await openPile()
    expect(screen.queryByRole("button", { name: /Show more/ })).toBeNull()
    expect(document.querySelector(".table-more")).toBeNull()
  })

  it("draws an empty list for an empty pile and reads no further", async () => {
    serve(worthServer({ pending: [], yes: [] }))
    await openPile()
    expect(rows()).toEqual([])
    expect(total()).toBe("0")
    expect(server.reads(TABLE)).toHaveLength(1)
  })

  it("says so when the pile cannot be read", async () => {
    server.table.mockImplementationOnce(() => errorResponse("review store is locked", 500))
    renderWorth("yes", server, { progress: PAGE })
    await screen.findByRole("heading", { name: "Could not load the review" })
    expect(screen.getByText("review store is locked")).toBeTruthy()
  })
})

// Point 2
describe("PileTable: only the rows near the viewport", () => {
  beforeEach(() => serve(worthServer({ pending: [], yes: pileOf(100) })))

  it("mounts the rows the viewport shows plus five more, between two spacers", async () => {
    await openPile()
    // 400px of 61px rows is seven rows; five more are kept past the edge.
    expect(rowNames()).toEqual(Array.from({ length: 12 }, (_, position) => named(position)))
    expect(rows().map((mounted) => mounted.getAttribute("data-index"))).toEqual(
      Array.from({ length: 12 }, (_, position) => String(position)),
    )
    expect([...table().children].map((child) => child.className)).toEqual([
      "virtual-spacer",
      ...Array.from({ length: 12 }, () => "decision-row"),
      "virtual-spacer",
    ])
    expect(spacers()).toEqual(["0px", `${88 * ROW_PX}px`])
    expect(table().querySelector(".virtual-spacer")?.getAttribute("aria-hidden")).toBe("true")
  })

  it("mounts the rows a scroll brings near and lets go of the ones left behind", async () => {
    await openPile()
    layout.scrollTo(list(), 50 * ROW_PX)
    await waitFor(() => expect(rowNames()[0]).toBe(named(45)))
    expect(rowNames().at(-1)).toBe(named(61))
    expect(spacers()).toEqual([`${45 * ROW_PX}px`, `${38 * ROW_PX}px`])
    expect(findRow(named(0))).toBeUndefined()
  })

  it("measures its rows: an opened row makes the list longer", async () => {
    await openPile()
    expect(list().scrollHeight).toBe(100 * ROW_PX)
    await toggle(named(1))
    layout.resize(row(named(1)))
    await waitFor(() => expect(list().scrollHeight).toBe(99 * ROW_PX + OPEN_ROW_PX))
    // The taller row leaves room for fewer: three in view and five past the edge.
    expect(rowNames()).toHaveLength(8)
  })

  it("keeps an opened row open, with what it read, when it scrolls out and back", async () => {
    await openPile()
    await toggle(named(3))
    await waitFor(() =>
      expect(details(named(3)).querySelector(".row-facts p")?.textContent).toBe("Met at Acme."),
    )
    expect([server.reads(DETAILS), server.reads(DOSSIER)]).toEqual([
      [`${DETAILS}?slug=p003-sample`],
      [`${DOSSIER}?slug=p003-sample&skip=1`],
    ])

    layout.scrollTo(list(), 50 * ROW_PX)
    await waitFor(() => expect(findRow(named(3))).toBeUndefined())
    layout.scrollTo(list(), 0)
    await waitFor(() => expect(findRow(named(3))).toBeTruthy())
    await act(() => wait(0))
    expect(row(named(3)).open).toBe(true)
    expect(details(named(3)).querySelector(".row-facts p")?.textContent).toBe("Met at Acme.")
    expect(row(named(2)).open).toBe(false)
    expect([server.reads(DETAILS), server.reads(DOSSIER)].map((reads) => reads.length)).toEqual([1, 1])
  })
})

// Point 3
describe("PileTable: the pages", () => {
  /** 45 people, read 20 a page: the first page (1220px) is longer than the 400px list. */
  beforeEach(() => serve(worthServer({ pending: [], yes: pileOf(45), pageSize: 20 })))

  it("reads the next page, from the rows held, once the viewport is within 120px of the end", async () => {
    await openPile()
    expect(server.reads(TABLE)).toEqual([`${TABLE}?view=yes&offset=0`])
    // 20 rows are 1220px: at 700 the end is 120px away, at 701 it is nearer.
    layout.scrollTo(list(), 700)
    await settle()
    expect(server.reads(TABLE)).toHaveLength(1)
    layout.scrollTo(list(), 701)
    await waitFor(() =>
      expect(server.reads(TABLE)).toEqual([`${TABLE}?view=yes&offset=0`, `${TABLE}?view=yes&offset=20`]),
    )
    await waitFor(() => expect(list().scrollHeight).toBe(40 * ROW_PX))
    await settle()
    // The new rows put the end out of reach again: nothing more is read.
    expect(server.reads(TABLE)).toHaveLength(2)
    expect(total()).toBe("45")
  })

  it("says Loading… while a page is on its way, and reads one page at a time", async () => {
    await openPile()
    const held = holdPage()
    layout.scrollTo(list(), 800)
    await waitFor(() => expect(loading().hidden).toBe(false))
    layout.scrollTo(list(), 810)
    layout.resize(list())
    await settle()
    expect(server.reads(TABLE)).toHaveLength(2)
    held.open()
    await waitFor(() => expect(loading().hidden).toBe(true))
    expect(list().scrollHeight).toBe(40 * ROW_PX)
  })

  it("stops once every row is held", async () => {
    await openPile()
    layout.scrollTo(list(), 20 * ROW_PX - 400)
    await waitFor(() => expect(list().scrollHeight).toBe(40 * ROW_PX))
    layout.scrollTo(list(), 40 * ROW_PX - 400)
    await waitFor(() => expect(list().scrollHeight).toBe(45 * ROW_PX))
    expect(
      server.reads(TABLE).map((url) => new URL(url, "http://review.test").searchParams.get("offset")),
    ).toEqual(["0", "20", "40"])
    layout.scrollTo(list(), 45 * ROW_PX - 400)
    layout.resize(list())
    await settle()
    expect(server.reads(TABLE)).toHaveLength(3)
    expect(rowNames().at(-1)).toBe(named(44))
  })

  it("stops when a page comes back empty: the pile ends at the rows held", async () => {
    // The pile shrank since its size was read: the server has 20 people, not 45.
    server.table.mockImplementationOnce(() => jsonResponse({ rows: pileOf(20), total: 45 }))
    server.table.mockImplementation(() => jsonResponse({ rows: [], total: 45 }))
    await openPile()
    expect(total()).toBe("45")
    layout.scrollTo(list(), 20 * ROW_PX - 400)
    await waitFor(() => expect(total()).toBe("20"))
    layout.scrollTo(list(), 20 * ROW_PX - 401)
    layout.resize(list())
    await settle()
    expect(server.reads(TABLE)).toEqual([`${TABLE}?view=yes&offset=0`, `${TABLE}?view=yes&offset=20`])
    expect(loading().hidden).toBe(true)
  })

  it("reads on right after mount, and after each page, until the rows fill the viewport", async () => {
    serve(worthServer({ pending: [], yes: pileOf(8), pageSize: 3 }))
    await openPile()
    await waitFor(() => expect(rowNames()).toHaveLength(8))
    expect(server.reads(TABLE)).toEqual([
      `${TABLE}?view=yes&offset=0`,
      `${TABLE}?view=yes&offset=3`,
      `${TABLE}?view=yes&offset=6`,
    ])
  })

  it("reads the next page when a resize brings the end of the rows near", async () => {
    await openPile()
    layout.box.height = 1200
    layout.resize(list())
    await waitFor(() => expect(server.reads(TABLE)).toHaveLength(2))
    expect(server.reads(TABLE)[1]).toBe(`${TABLE}?view=yes&offset=20`)
  })

  it("says so when a page cannot be read, and reads it again on the next scroll", async () => {
    const { toastError } = await openPile()
    server.table.mockImplementationOnce(() => errorResponse("offset must be an integer", 400))
    layout.scrollTo(list(), 800)
    await waitFor(() => expect(toastError.mock.calls).toEqual([["offset must be an integer"]]))
    await waitFor(() => expect(loading().hidden).toBe(true))
    await settle()
    // No retry on its own.
    expect(server.reads(TABLE)).toHaveLength(2)
    expect(list().scrollHeight).toBe(20 * ROW_PX)
    layout.scrollTo(list(), 810)
    await waitFor(() => expect(list().scrollHeight).toBe(40 * ROW_PX))
    expect(server.reads(TABLE)[2]).toBe(`${TABLE}?view=yes&offset=20`)
  })
})

// T2
describe("PileTable: a collapsed row", () => {
  it("draws the caret, the initials, the name, the labels and the flip button", async () => {
    await openPile()
    const collapsed = row("Avery Fox")
    expect(collapsed.open).toBe(false)
    const summary = must(collapsed.querySelector("summary.decision-row-summary"))
    expect([...summary.children].map((child) => child.className)).toEqual([
      "decision-row-caret",
      "avatar",
      "decision-row-main",
      "decision-row-actions",
    ])
    expect(summary.querySelector(".decision-row-caret")?.getAttribute("aria-hidden")).toBe("true")
    // A pile's row carries no profile: the avatar is the person's initials, never a picture.
    expect(summary.querySelector(".avatar")?.textContent).toBe("AF")
    expect(summary.querySelector(".avatar img")).toBeNull()
    const line = must(summary.querySelector(".decision-row-main > .person-name-line"))
    expect(line.querySelector("strong")?.textContent).toBe("Avery Fox")
    expect([...line.querySelectorAll(".person-label")].map((label) => label.textContent)).toEqual([
      "Founder",
      "Close friend",
    ])
    const flip = flipButton("Avery Fox")
    expect(flip.textContent).toBe("No")
    expect(flip.getAttribute("aria-label")).toBe("Mark Avery Fox No")
    expect(flip.className).toBe("button button-ghost")
    expect(summary.querySelectorAll("button")).toHaveLength(1)
  })

  it("flips to Yes on the No pile", async () => {
    await openPile("no")
    const flip = flipButton("Morgan Hale")
    expect(flip.textContent).toBe("Yes")
    expect(flip.getAttribute("aria-label")).toBe("Mark Morgan Hale Yes")
  })

  it("calls a person with no name This person", async () => {
    const nameless = rowNamed("Avery Fox")
    serve(worthServer({ pending: [], yes: [{ ...nameless, person: { ...nameless.person, name: "" } }] }))
    await openPile()
    expect(rowNames()).toEqual(["This person"])
    expect(flipButton("This person").getAttribute("aria-label")).toBe("Mark This person No")
  })
})

// Point 5 (T3)
describe("PileTable: an opened row", () => {
  it("asks for nothing and shows only the reason until it is opened", async () => {
    await openPile()
    expect([server.reads(DETAILS), server.reads(DOSSIER)]).toEqual([[], []])
    expect([...detail("Avery Fox").children].map((child) => child.className)).toEqual([
      "row-facts",
      "dossier-text",
      "scroll-cue",
    ])
    expect(facts(detail("Avery Fox"), ":scope > dl.row-facts")).toEqual([["Why yes", "You said yes"]])
    expect(details("Avery Fox").childElementCount).toBe(0)
    expect(details("Avery Fox").textContent).toBe("")
    expect(details("Avery Fox").getAttribute("aria-busy")).toBe("true")
  })

  it("reads the person's details and dossier, then shows the card, Who they are and the dossier", async () => {
    await openPile()
    server.dossier.mockImplementationOnce(() => new Response("<h3>Summary</h3><p>Met at Acme.</p>"))
    await toggle("Avery Fox")
    const loaded = details("Avery Fox")
    await waitFor(() => expect(loaded.querySelector(".profile-card")).toBeTruthy())
    expect(loaded.hasAttribute("aria-busy")).toBe(false)
    expect([...loaded.children].map((child) => `${child.tagName.toLowerCase()}.${child.className}`)).toEqual([
      "div.profile-card",
      "section.details",
      "h4.dossier-heading",
      "div.row-facts",
    ])
    // The profile card is drawn from the answer: the row itself carries no sources or profile.
    const card = must(loaded.querySelector(".profile-card"))
    expect(card.querySelector(".avatar")?.textContent).toBe("AF")
    expect(card.querySelector(".avatar img")?.getAttribute("src")).toBe(
      "https://media.example.com/jordan-bravo.jpg",
    )
    expect([...card.querySelectorAll(".source")].map((badge) => badge.textContent)).toEqual([
      "Gmail",
      "iMessage",
    ])
    expect(card.querySelector("h2")?.textContent).toBe("Avery Fox")
    expect([...card.querySelectorAll(".person-label")].map((label) => label.textContent)).toEqual([
      "Founder",
      "Close friend",
    ])
    const link = must(card.querySelector("a.linkedin-label"))
    expect(link.textContent).toBe("View LinkedIn↗")
    expect(link.getAttribute("href")).toBe("https://www.linkedin.com/in/jordan-bravo")
    expect(facts(loaded, "section.details > dl")).toEqual([
      ["Contact", "jordan@example.com · +15550100"],
      ["Summary", "Founder at Example Labs"],
      ["Location", "Springfield"],
      ["Work", "Founder, Example LabsEngineer, Acme"],
      ["Education", "Example University"],
    ])
    expect(loaded.querySelector("h4.dossier-heading")?.textContent).toBe("Who they are")
    expect(loaded.querySelector(":scope > .row-facts")?.innerHTML).toBe("<h3>Summary</h3><p>Met at Acme.</p>")
    // The reason leads the detail; the row has no name header or Contact fact of its own.
    expect(facts(detail("Avery Fox"), ":scope > dl.row-facts")).toEqual([["Why yes", "You said yes"]])
    expect(detail("Avery Fox").querySelector(".decision-expanded-profile")).toBeNull()
    // One read of each: the card inside the row does not ask for the dossier again.
    expect(server.reads(DETAILS)).toEqual([`${DETAILS}?slug=avery-fox`])
    expect(server.reads(DOSSIER)).toEqual([`${DOSSIER}?slug=avery-fox&skip=1`])
    expect(loaded.querySelector(".details .dossier-text")).toBeNull()
  })

  it.each([
    ["the details", () => server.details],
    ["the dossier", () => server.dossier],
  ] as const)("says Loading… and draws nothing while %s are still on their way", async (_what, route) => {
    await openPile()
    const held = gate()
    const answer = route().getMockImplementation()
    route().mockImplementationOnce(async (query) => {
      await held.opened
      return must(answer)(query)
    })
    await toggle("Avery Fox")
    await waitFor(() => expect(details("Avery Fox").textContent).toBe("Loading…"))
    await act(() => wait(20))
    // The other read has landed; the row still waits for both and draws them at once.
    expect(details("Avery Fox").textContent).toBe("Loading…")
    expect(details("Avery Fox").childElementCount).toBe(0)
    expect(details("Avery Fox").getAttribute("aria-busy")).toBe("true")
    held.open()
    await waitFor(() => expect(details("Avery Fox").querySelector(".profile-card")).toBeTruthy())
    expect(details("Avery Fox").querySelector(".row-facts p")?.textContent).toBe("Met at Acme.")
  })

  it("reads a row's details once, however often it is opened", async () => {
    await openPile()
    await toggle("Avery Fox")
    await waitFor(() => expect(details("Avery Fox").querySelector(".profile-card")).toBeTruthy())
    await toggle("Avery Fox")
    await toggle("Avery Fox")
    expect(details("Avery Fox").querySelector(".row-facts p")?.textContent).toBe("Met at Acme.")
    expect([server.reads(DETAILS), server.reads(DOSSIER)].map((reads) => reads.length)).toEqual([1, 1])

    // Another row reads its own.
    await toggle("Blake Gray")
    await waitFor(() => expect(server.reads(DETAILS)[1]).toBe(`${DETAILS}?slug=blake-gray`))
    expect(server.reads(DOSSIER)[1]).toBe(`${DOSSIER}?slug=blake-gray&skip=1`)
  })

  it.each([
    ["the person is gone", () => server.details.mockImplementation(() => errorResponse("gone", 404))],
    [
      "the dossier is refused",
      () => server.dossier.mockImplementation(() => new Response("", { status: 500 })),
    ],
  ])("says No details found when %s, and does not ask again", async (_when, refuse) => {
    refuse()
    await openPile()
    await toggle("Avery Fox")
    await waitFor(() => expect(details("Avery Fox").textContent).toBe("No details found"))
    expect(details("Avery Fox").childElementCount).toBe(0)
    expect(details("Avery Fox").hasAttribute("aria-busy")).toBe(false)
    await toggle("Avery Fox")
    await toggle("Avery Fox")
    expect(details("Avery Fox").textContent).toBe("No details found")
    expect(server.reads(DETAILS)).toHaveLength(1)
  })

  it.each([
    ["the details", () => server.details],
    ["the dossier", () => server.dossier],
  ] as const)("says Could not load details when the read of %s fails", async (_what, route) => {
    route().mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")))
    await openPile()
    await toggle("Avery Fox")
    await waitFor(() => expect(details("Avery Fox").textContent).toBe("Could not load details"))
    expect(details("Avery Fox").hasAttribute("aria-busy")).toBe(false)
  })

  it("says Why no on the No pile, and draws a person with no profile from the parent alone", async () => {
    await openPile("no")
    const person = personNamed("Morgan Hale", { sources: ["whatsapp"], contacts: "+15550100" })
    server.details.mockImplementationOnce(() => jsonResponse(worthDetails({ person, candidate: null })))
    await toggle("Morgan Hale")
    expect(facts(detail("Morgan Hale"), ":scope > dl.row-facts")).toEqual([["Why no", "Not worth adding"]])
    const loaded = details("Morgan Hale")
    await waitFor(() => expect(loaded.querySelector(".profile-card h2")?.textContent).toBe("Morgan Hale"))
    expect([...loaded.querySelectorAll(".source")].map((badge) => badge.textContent)).toEqual(["WhatsApp"])
    expect(loaded.querySelector(".avatar img")).toBeNull()
    expect(facts(loaded, "section.details > dl")).toEqual([["Contact", "+15550100"]])
    expect(loaded.querySelector("a.linkedin-label")).toBeNull()
  })

  it("shows the scroll cue while the opened row runs past its fold", async () => {
    await openPile()
    const box = detail("Avery Fox")
    const cue = must(box.querySelector<HTMLButtonElement>(":scope > button.scroll-cue"))
    expect(cue.hidden).toBe(true)
    expect(cue.textContent).toBe("⌄")
    expect(cue.getAttribute("aria-label")).toBe("Scroll down")

    // Opening the row is what gives the detail its height: the cue measures on toggle.
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 420 })
    Object.defineProperty(box, "scrollHeight", { configurable: true, value: 900 })
    await toggle("Avery Fox")
    await waitFor(() => expect(cue.hidden).toBe(false))

    const scrollBy = vi.fn()
    box.scrollBy = scrollBy
    fireEvent.click(cue)
    expect(scrollBy).toHaveBeenCalledWith({ top: 294, behavior: "auto" })

    Object.defineProperty(box, "scrollTop", { configurable: true, value: 480 })
    fireEvent.scroll(box)
    await waitFor(() => expect(cue.hidden).toBe(true))
  })
})

// Point 4 (T4)
describe("PileTable: a flip", () => {
  it("moves a Yes row to No at once: the row fades, both counts move, the save runs", async () => {
    const { toast, toastError, applyProgress } = await openPile()
    const held = server.holdSave()
    expect(tabText()).toEqual(["Review4", "Yes9", "No7"])

    fireEvent.click(flipButton("Avery Fox"))
    expect(row("Avery Fox").className).toBe("decision-row leaving")
    expect(flipButton("Avery Fox").disabled).toBe(true)
    expect(row("Blake Gray").className).toBe("decision-row")
    expect(tabText()).toEqual(["Review4", "Yes8", "No8"])
    // The button sits in the summary: pressing it must not open the row.
    expect(row("Avery Fox").open).toBe(false)
    await waitFor(() =>
      expect(server.saves()).toEqual([{ pub: "worth-avery", worth: "no", parent_slug: "avery-fox" }]),
    )
    expect(toast).not.toHaveBeenCalled()
    expect(total()).toBe("5")

    held.open()
    await waitFor(() =>
      expect(rowNames()).toEqual(["Blake Gray", "Casey Delta", "Jordan Bravo", "Riley Echo"]),
    )
    expect(total()).toBe("4")
    expect(toast.mock.calls).toEqual([["Rejected"]])
    expect(applyProgress.mock.calls).toEqual([[server.progress()]])
    expect(tabText()).toEqual(["Review0", "Yes4", "No3"])
    expect(toastError).not.toHaveBeenCalled()
  })

  it("moves a No row to Yes and says Added", async () => {
    const { toast } = await openPile("no")
    fireEvent.click(flipButton("Quinn Ives"))
    expect(tabText()).toEqual(["Review4", "Yes10", "No6"])
    await waitFor(() => expect(rowNames()).toEqual(["Morgan Hale"]))
    expect(server.saves()).toEqual([{ pub: "worth-quinn", worth: "yes", parent_slug: "quinn-ives" }])
    expect(toast.mock.calls).toEqual([["Added"]])
    expect(tabText()).toEqual(["Review0", "Yes6", "No1"])
    expect(total()).toBe("1")
  })

  it("reads the next page when the rows left no longer reach past the viewport", async () => {
    // Nine of twenty rows (549px) end 149px below the 400px viewport; eight end 88px below.
    serve(worthServer({ pending: [], yes: pileOf(20), pageSize: 9 }))
    await openPile()
    expect(server.reads(TABLE)).toHaveLength(1)
    fireEvent.click(flipButton(named(0)))
    await waitFor(() => expect(total()).toBe("19"))
    // The flipped row left the pile on the server too: the page starts at the eight rows held.
    await waitFor(() => expect(server.reads(TABLE)[1]).toBe(`${TABLE}?view=yes&offset=8`))
    await waitFor(() => expect(list().scrollHeight).toBe(17 * ROW_PX))
    expect(rowNames()[0]).toBe(named(1))
    expect(rowNames()).toContain(named(9))
  })

  it("keeps a page read and a flip apart, so a page never starts at a row count the flip changed", async () => {
    serve(worthServer({ pending: [], yes: pileOf(45), pageSize: 20 }))
    await openPile()
    const page = holdPage()
    layout.scrollTo(list(), 800)
    await waitFor(() => expect(loading().hidden).toBe(false))

    // A row is flipped while that page is on its way: its save waits for the page to land.
    const save = server.holdSave()
    fireEvent.click(flipButton(named(15)))
    await settle()
    expect(row(named(15)).className).toBe("decision-row leaving")
    expect(server.saves()).toEqual([])
    page.open()
    await waitFor(() => expect(server.saves()).toHaveLength(1))
    await waitFor(() => expect(list().scrollHeight).toBe(40 * ROW_PX))

    // While the save is out, the end of the rows is in reach but no page is read.
    layout.scrollTo(list(), 2000)
    await settle()
    expect(server.reads(TABLE)).toHaveLength(2)

    // Saved: the row left the pile on the server, and the next page starts at the 39 held.
    save.open()
    await waitFor(() => expect(total()).toBe("44"))
    await waitFor(() => expect(server.reads(TABLE)[2]).toBe(`${TABLE}?view=yes&offset=39`))
  })

  it("reads no page while a saved row is still fading out of the rows held", async () => {
    serve(worthServer({ pending: [], yes: pileOf(45), pageSize: 20 }))
    await openPile("yes", { fadeMs: 300 })
    fireEvent.click(flipButton(named(3)))
    await waitFor(() => expect(server.saves()).toHaveLength(1))
    // The save has answered; the row is still on screen, fading. The end is in reach.
    layout.scrollTo(list(), 800)
    await settle()
    expect(row(named(15)).isConnected).toBe(true)
    expect(server.reads(TABLE)).toHaveLength(1)
    // Once the row is gone the page starts at the 19 rows held.
    await waitFor(() => expect(server.reads(TABLE)[1]).toBe(`${TABLE}?view=yes&offset=19`))
  })

  // P0.3
  it("posts exactly once however often the flip is pressed", async () => {
    await openPile()
    const held = server.holdSave()
    fireEvent.click(flipButton("Avery Fox"))
    fireEvent.click(flipButton("Avery Fox"))
    fireEvent.click(flipButton("Avery Fox"))
    held.open()
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    expect(server.fetch.mock.calls.filter(([url]) => url === "/worth")).toHaveLength(1)
  })

  // P0.4
  it("brings the row and both counts back when the save is refused, and says why", async () => {
    const { toast, toastError, applyProgress } = await openPile()
    const held = server.holdSave(() => refusal("review store is locked"))
    fireEvent.click(flipButton("Avery Fox"))
    expect(tabText()).toEqual(["Review4", "Yes8", "No8"])

    held.open()
    await waitFor(() => expect(toastError.mock.calls).toEqual([["review store is locked"]]))
    expect(rowNames()).toHaveLength(5)
    expect(row("Avery Fox").className).toBe("decision-row")
    expect(flipButton("Avery Fox").disabled).toBe(false)
    expect(tabText()).toEqual(["Review4", "Yes9", "No7"])
    expect(total()).toBe("5")
    expect(toast).not.toHaveBeenCalled()
    expect(applyProgress).not.toHaveBeenCalled()
    expect(server.piles.yes).toHaveLength(5)
  })

  it("keeps a saved row until its fade is over, and brings a refused one back at once", async () => {
    const { toastError } = await openPile("yes", { fadeMs: 150 })
    fireEvent.click(flipButton("Avery Fox"))
    await waitFor(() => expect(server.piles.no).toHaveLength(3))
    await act(() => wait(60))
    expect(row("Avery Fox").className).toBe("decision-row leaving")
    await waitFor(() => expect(findRow("Avery Fox")).toBeUndefined())

    server.worth.mockImplementationOnce(() => refusal("review store is locked"))
    fireEvent.click(flipButton("Blake Gray"))
    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1))
    expect(row("Blake Gray").className).toBe("decision-row")
  })

  it("does nothing more once the stage has left the screen", async () => {
    const { toast, applyProgress, unmount } = await openPile()
    const held = server.holdSave()
    fireEvent.click(flipButton("Avery Fox"))
    await waitFor(() => expect(server.worth).toHaveBeenCalledTimes(1))
    unmount()
    held.open()
    await waitFor(() => expect(server.piles.no).toHaveLength(3))
    await wait(20)
    expect(toast).not.toHaveBeenCalled()
    expect(applyProgress).not.toHaveBeenCalled()
  })
})
