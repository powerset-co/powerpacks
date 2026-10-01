import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { wait } from "@/lib/review/timing"
import { errorResponse, motionMedia, pageProgress } from "@/testing/review-fixture"
import type { WorthTab } from "@/types/review"

import type { Review } from "../hooks/useReview"
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
/** What the page loaded with; the server's own counts differ, so a corrected count shows. */
const PAGE = pageProgress({ worth_pending: 4, worth_yes: 9, worth_no: 7 })

let server: WorthServer

/** Five people on Yes and two on No, read two rows a page. */
function decidedServer() {
  return worthServer({
    pending: [],
    yes: ["Avery Fox", "Blake Gray", "Casey Delta", "Jordan Bravo", "Riley Echo"].map((name) =>
      rowNamed(name),
    ),
    no: [
      rowNamed("Morgan Hale", { reason: "Not worth adding" }),
      rowNamed("Quinn Ives", { reason: "You said no" }),
    ],
    pageSize: 2,
  })
}

function serve(next: WorthServer) {
  server = next
  vi.stubGlobal("fetch", server.fetch)
}

beforeEach(() => {
  serve(decidedServer())
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const rows = () => [...document.querySelectorAll<HTMLDetailsElement>("details.decision-row")]
const rowNames = () => rows().map((row) => row.querySelector("summary strong")?.textContent)
const row = (name: string) =>
  must(
    rows().find((candidate) => candidate.querySelector("summary strong")?.textContent === name),
    `${name}'s row`,
  )
const flipButton = (name: string) => must(row(name).querySelector<HTMLButtonElement>("summary button"))
const moreButton = () => screen.queryByRole<HTMLButtonElement>("button", { name: /^Show more/ })
const facts = (name: string) =>
  [...row(name).querySelectorAll("dl.row-facts > div")].map((fact) => [
    fact.querySelector("dt")?.textContent,
    fact.querySelector("dd")?.textContent,
  ])

/** A decided pile's table with its first page on screen. */
async function openPile(pile: Exclude<WorthTab, "review"> = "yes", overrides: Partial<Review> = {}) {
  const view = renderWorth(pile, server, { progress: PAGE, ...overrides })
  await waitFor(() => expect(document.querySelector(".decision-table")).toBeTruthy())
  return view
}

/** Opens (or closes) a row the way a click on its summary does. */
async function toggle(name: string) {
  const details = row(name)
  const open = !details.open
  fireEvent.click(must(details.querySelector("summary")))
  await waitFor(() => expect(details.open).toBe(open))
  // jsdom fires `toggle` a task later.
  await wait(0)
}

// T1
describe("PileTable: the pages", () => {
  it("reads one page of the pile and keeps the server's order", async () => {
    const { container } = await openPile()
    expect(server.reads(TABLE)).toEqual([`${TABLE}?view=yes&offset=0`])
    expect(rowNames()).toEqual(["Avery Fox", "Blake Gray"])
    const table = must(container.querySelector(".worth-panel > .decision-table"))
    expect(table.getAttribute("data-view")).toBe("yes")
    const more = must(moreButton())
    expect(more.textContent).toBe("Show more (3 left)")
    expect(more.className).toBe("button button-outline table-more")
    expect(more.previousElementSibling).toBe(table)
  })

  it("appends the next page on Show more and counts what is left, until nothing is", async () => {
    await openPile()
    const held = gate()
    server.table.mockImplementationOnce(async (query) => {
      await held.opened
      return server.readTable(query)
    })
    fireEvent.click(must(moreButton()))
    expect(must(moreButton()).disabled).toBe(true)
    held.open()
    await waitFor(() =>
      expect(rowNames()).toEqual(["Avery Fox", "Blake Gray", "Casey Delta", "Jordan Bravo"]),
    )
    expect(must(moreButton()).textContent).toBe("Show more (1 left)")
    expect(must(moreButton()).disabled).toBe(false)

    fireEvent.click(must(moreButton()))
    await waitFor(() => expect(rowNames()).toHaveLength(5))
    expect(rowNames()[4]).toBe("Riley Echo")
    expect(moreButton()).toBeNull()
    expect(server.reads(TABLE)).toEqual([
      `${TABLE}?view=yes&offset=0`,
      `${TABLE}?view=yes&offset=2`,
      `${TABLE}?view=yes&offset=4`,
    ])
  })

  it("has no Show more when the first page is the whole pile", async () => {
    await openPile("no")
    expect(rowNames()).toEqual(["Morgan Hale", "Quinn Ives"])
    expect(moreButton()).toBeNull()
  })

  it("draws an empty table for an empty pile", async () => {
    serve(worthServer({ pending: [], yes: [] }))
    const { container } = await openPile()
    expect(must(container.querySelector(".decision-table")).childElementCount).toBe(0)
    expect(moreButton()).toBeNull()
  })

  it("says so when a page cannot be read, and lets Show more be pressed again", async () => {
    const { toastError } = await openPile()
    server.table.mockImplementationOnce(() => errorResponse("offset must be a number", 400))
    fireEvent.click(must(moreButton()))
    await waitFor(() => expect(toastError.mock.calls).toEqual([["offset must be a number"]]))
    expect(rowNames()).toEqual(["Avery Fox", "Blake Gray"])
    expect(must(moreButton()).disabled).toBe(false)
    expect(must(moreButton()).textContent).toBe("Show more (3 left)")
    fireEvent.click(must(moreButton()))
    await waitFor(() => expect(rowNames()).toHaveLength(4))
  })

  it("says so when the pile cannot be read", async () => {
    server.table.mockImplementationOnce(() => errorResponse("review store is locked", 500))
    renderWorth("yes", server, { progress: PAGE })
    await screen.findByRole("heading", { name: "Could not load the review" })
    expect(screen.getByText("review store is locked")).toBeTruthy()
  })
})

// T2
describe("PileTable: a collapsed row", () => {
  it("draws the caret, the avatar, the name, the labels and the flip button", async () => {
    await openPile()
    const details = row("Avery Fox")
    expect(details.open).toBe(false)
    const summary = must(details.querySelector("summary.decision-row-summary"))
    expect([...summary.children].map((child) => child.className)).toEqual([
      "decision-row-caret",
      "avatar",
      "decision-row-main",
      "decision-row-actions",
    ])
    expect(summary.querySelector(".decision-row-caret")?.getAttribute("aria-hidden")).toBe("true")
    expect(summary.querySelector(".avatar")?.textContent).toBe("AF")
    expect(summary.querySelector(".avatar img")?.getAttribute("src")).toBe("/api/avatar?pub=avery-fox-1")
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

  it("flips to Yes on the No table", async () => {
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

// T3
describe("PileTable: an open row", () => {
  it("shows the name, the labels, the contact and why the person is in the pile", async () => {
    await openPile()
    await toggle("Avery Fox")
    const detail = must(row("Avery Fox").querySelector(".decision-row-detail"))
    const profile = must(detail.querySelector(".decision-expanded-profile"))
    expect(profile.querySelector("h2")?.textContent).toBe("Avery Fox")
    expect([...profile.querySelectorAll(".person-label")].map((label) => label.textContent)).toEqual([
      "Founder",
      "Close friend",
    ])
    expect(facts("Avery Fox")).toEqual([
      ["Contact", "jordan@example.com · +15550100"],
      ["Why yes", "You said yes"],
    ])
    expect(detail.querySelector("h4.dossier-heading")?.textContent).toBe("Who they are")
  })

  it("says Why no on the No pile and leaves Contact out when the person has none", async () => {
    serve(
      worthServer({
        pending: [],
        no: [{ person: personNamed("Morgan Hale"), candidate: null, reason: "Not worth adding" }],
      }),
    )
    await openPile("no")
    await toggle("Morgan Hale")
    expect(facts("Morgan Hale")).toEqual([["Why no", "Not worth adding"]])
  })

  it("asks for the dossier the first time the row opens, and only then", async () => {
    await openPile()
    expect(server.reads("/api/dossier")).toEqual([])
    expect(row("Avery Fox").querySelector(".dossier-text")).toBeNull()

    const held = gate()
    server.dossier.mockImplementationOnce(async () => {
      await held.opened
      return new Response("<h3>Summary</h3><p>Met at Acme.</p>")
    })
    await toggle("Avery Fox")
    const dossier = () => must(row("Avery Fox").querySelector(".decision-row-detail > .dossier-text"))
    await waitFor(() => expect(dossier().textContent).toBe("Loading…"))
    expect(dossier().className).toBe("dossier-text row-facts")
    held.open()
    await waitFor(() => expect(dossier().querySelector("p")?.textContent).toBe("Met at Acme."))
    expect(server.reads("/api/dossier")).toEqual(["/api/dossier?slug=avery-fox&skip=1"])

    await toggle("Avery Fox")
    await toggle("Avery Fox")
    expect(dossier().querySelector("p")?.textContent).toBe("Met at Acme.")
    expect(server.reads("/api/dossier")).toHaveLength(1)

    // Another row asks for its own.
    await toggle("Blake Gray")
    await waitFor(() => expect(server.reads("/api/dossier")[1]).toBe("/api/dossier?slug=blake-gray&skip=1"))
  })

  it("says No details found when the server has no dossier", async () => {
    server.dossier.mockImplementation(() => new Response("", { status: 404 }))
    await openPile()
    await toggle("Avery Fox")
    await waitFor(() =>
      expect(row("Avery Fox").querySelector(".dossier-text")?.textContent).toBe("No details found"),
    )
  })

  it("says Could not load details when the dossier request fails", async () => {
    server.dossier.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")))
    await openPile()
    await toggle("Avery Fox")
    await waitFor(() =>
      expect(row("Avery Fox").querySelector(".dossier-text")?.textContent).toBe("Could not load details"),
    )
  })

  it("shows the scroll cue while the open row runs past its fold", async () => {
    await openPile()
    const detail = must(row("Avery Fox").querySelector<HTMLElement>(".decision-row-detail"))
    const cue = must(detail.querySelector<HTMLButtonElement>(":scope > button.scroll-cue"))
    expect(cue.hidden).toBe(true)
    expect(cue.textContent).toBe("⌄")
    expect(cue.getAttribute("aria-label")).toBe("Scroll down")

    // Opening the row is what gives the detail its height: the cue measures on toggle.
    server.dossier.mockImplementation(() => new Promise<Response>(() => undefined))
    Object.defineProperty(detail, "clientHeight", { configurable: true, value: 420 })
    Object.defineProperty(detail, "scrollHeight", { configurable: true, value: 900 })
    await toggle("Avery Fox")
    await waitFor(() => expect(cue.hidden).toBe(false))

    const scrollBy = vi.fn()
    detail.scrollBy = scrollBy
    fireEvent.click(cue)
    expect(scrollBy).toHaveBeenCalledWith({ top: 294, behavior: "auto" })

    Object.defineProperty(detail, "scrollTop", { configurable: true, value: 480 })
    fireEvent.scroll(detail)
    await waitFor(() => expect(cue.hidden).toBe(true))
  })
})

// T4
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

    held.open()
    await waitFor(() => expect(rowNames()).toEqual(["Blake Gray"]))
    expect(toast.mock.calls).toEqual([["Rejected"]])
    expect(applyProgress.mock.calls).toEqual([[server.progress()]])
    expect(tabText()).toEqual(["Review0", "Yes4", "No3"])
    expect(toastError).not.toHaveBeenCalled()
    // What is left to show did not change: the flipped row was already on screen.
    expect(must(moreButton()).textContent).toBe("Show more (3 left)")
  })

  it("moves a No row to Yes and says Added", async () => {
    const { toast } = await openPile("no")
    fireEvent.click(flipButton("Quinn Ives"))
    expect(tabText()).toEqual(["Review4", "Yes10", "No6"])
    await waitFor(() => expect(rowNames()).toEqual(["Morgan Hale"]))
    expect(server.saves()).toEqual([{ pub: "worth-quinn", worth: "yes", parent_slug: "quinn-ives" }])
    expect(toast.mock.calls).toEqual([["Added"]])
    expect(tabText()).toEqual(["Review0", "Yes6", "No1"])
  })

  // P0.3
  it("posts exactly once however often the flip is pressed", async () => {
    await openPile()
    const held = server.holdSave()
    fireEvent.click(flipButton("Avery Fox"))
    fireEvent.click(flipButton("Avery Fox"))
    fireEvent.click(flipButton("Avery Fox"))
    held.open()
    await waitFor(() => expect(rowNames()).toEqual(["Blake Gray"]))
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
    expect(rowNames()).toEqual(["Avery Fox", "Blake Gray"])
    expect(row("Avery Fox").className).toBe("decision-row")
    expect(flipButton("Avery Fox").disabled).toBe(false)
    expect(tabText()).toEqual(["Review4", "Yes9", "No7"])
    expect(toast).not.toHaveBeenCalled()
    expect(applyProgress).not.toHaveBeenCalled()
    expect(server.piles.yes).toHaveLength(5)
  })

  it("keeps a saved row until its fade is over, and brings a refused one back at once", async () => {
    const { toastError } = await openPile("yes", { fadeMs: 150 })
    fireEvent.click(flipButton("Avery Fox"))
    await waitFor(() => expect(server.piles.no).toHaveLength(3))
    await wait(60)
    expect(row("Avery Fox").className).toBe("decision-row leaving")
    await waitFor(() => expect(rowNames()).toEqual(["Blake Gray"]))

    server.worth.mockImplementationOnce(() => refusal("review store is locked"))
    fireEvent.click(flipButton("Blake Gray"))
    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1))
    expect(row("Blake Gray").className).toBe("decision-row")
  })

  it("reads the next page from the rows still held once a row has left the pile", async () => {
    await openPile()
    fireEvent.click(flipButton("Avery Fox"))
    await waitFor(() => expect(rowNames()).toEqual(["Blake Gray"]))
    fireEvent.click(must(moreButton()))
    await waitFor(() => expect(rowNames()).toEqual(["Blake Gray", "Casey Delta", "Jordan Bravo"]))
    expect(server.reads(TABLE)[1]).toBe(`${TABLE}?view=yes&offset=1`)
    expect(must(moreButton()).textContent).toBe("Show more (1 left)")
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
