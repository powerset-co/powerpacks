import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { wait } from "@/lib/review/timing"
import {
  errorResponse,
  jsonResponse,
  motionMedia,
  pageProgress,
  reviewCandidate,
  worthCard,
} from "@/testing/review-fixture"

import type { Review } from "../hooks/useReview"
import {
  gate,
  personNamed,
  refusal,
  renderWorth,
  tabText,
  worthServer,
  type WorthServer,
} from "./worth-fixture"

const CARD = "/api/review/worth-card"
const NAMES = "/api/review/worth-pending"
/** What the page loaded with; the server's own counts differ, so a corrected count shows. */
const PAGE = pageProgress({ worth_pending: 3, worth_yes: 5, worth_no: 2 })

let server: WorthServer

beforeEach(() => {
  server = worthServer()
  vi.stubGlobal("fetch", server.fetch)
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const shows = (name: string) => screen.findByRole("heading", { level: 2, name })
const frame = () => must(document.querySelector<HTMLElement>(".worth-card"), "the card")
const answer = (label: "Yes" | "No") => screen.getByRole<HTMLButtonElement>("button", { name: label })
const noteBox = () => must(document.querySelector<HTMLTextAreaElement>(".worth-why textarea"), "the note box")
const searchBox = () => screen.findByRole<HTMLInputElement>("searchbox")
const listed = () => [...document.querySelectorAll(".worth-search-list li")].map((item) => item.textContent)

/** The queue on the review tab, its first card read and the card after it asked for. */
async function openQueue(overrides: Partial<Review> = {}, first = "Casey Delta") {
  const view = renderWorth("review", server, { progress: PAGE, ...overrides })
  await shows(first)
  await waitFor(() => expect(server.reads(CARD).length).toBeGreaterThan(1))
  return view
}

/** Every card read past the first fails until the returned function is called. */
function failCardReads() {
  let failing = true
  server.card.mockImplementation((query) =>
    failing && query.has("exclude") ? errorResponse("boom", 500) : server.readCard(query),
  )
  return () => {
    failing = false
  }
}

async function search(text: string) {
  const input = await searchBox()
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value: text } })
  return input
}

describe("WorthQueue: the card", () => {
  // W2
  it("reads the first card for the URL and draws the person, the note box and No / Yes", async () => {
    const { container } = await openQueue()
    expect(server.reads(CARD)[0]).toBe(CARD)
    const card = frame()
    expect(card.className).toBe("decision-card identity-card worth-card")
    expect(card.parentElement?.className).toBe("worth-panel")
    expect([...card.children].map((child) => child.className)).toEqual([
      "identity-scroll-shell",
      "worth-why",
      "binary-actions",
    ])
    const person = must(card.querySelector(".identity-scroll"))
    expect([...person.querySelectorAll(".source")].map((badge) => badge.textContent)).toEqual([
      "Gmail",
      "iMessage",
    ])
    expect(person.querySelector("h2")?.textContent).toBe("Casey Delta")
    expect([...person.querySelectorAll(".person-label")].map((label) => label.textContent)).toEqual([
      "Founder",
      "Close friend",
    ])
    expect(screen.getByRole("link", { name: "View LinkedIn" }).getAttribute("href")).toBe(
      "https://www.linkedin.com/in/jordan-bravo",
    )
    expect(person.querySelector(".avatar img")?.getAttribute("src")).toBe(
      "https://media.example.com/jordan-bravo.jpg",
    )
    expect([...person.querySelectorAll(".details > dl dt")].map((term) => term.textContent)).toEqual([
      "Contact",
      "Summary",
      "Location",
      "Work",
      "Education",
    ])
    const buttons = [...container.querySelectorAll<HTMLButtonElement>(".binary-actions button")]
    expect(buttons.map((button) => [button.textContent, button.className, button.disabled])).toEqual([
      ["No", "button button-outline", false],
      ["Yes", "button button-primary", false],
    ])
  })

  it("names the card by the candidate, else the parent, else nobody", async () => {
    const parent = personNamed("Casey Delta")
    const cards = [
      { person: parent, candidate: reviewCandidate({ name: "Casey D. Delta" }) },
      { person: parent, candidate: null },
      { person: { ...parent, name: "" }, candidate: null },
    ]
    for (const [position, expected] of ["Casey D. Delta", "Casey Delta", "This person"].entries()) {
      server.card.mockImplementationOnce(() => jsonResponse(worthCard({ card: cards[position] })))
      const view = renderWorth("review", server, { progress: PAGE })
      await shows(expected)
      view.unmount()
    }
  })

  // W3
  it("folds a long Work list behind show more / show fewer", async () => {
    const candidate = reviewCandidate({ name: "Casey Delta", experiences: ["a", "b", "c", "d", "e"] })
    const card = { person: personNamed("Casey Delta"), candidate }
    server.card.mockImplementationOnce(() => jsonResponse(worthCard({ card })))
    renderWorth("review", server, { progress: PAGE })
    await shows("Casey Delta")
    const work = must(frame().querySelector(".fact-list"), "the Work list")
    const shownItems = () => [...work.querySelectorAll("li:not([hidden])")]
    expect(shownItems()).toHaveLength(3)
    fireEvent.click(screen.getByRole("button", { name: "+ show 2 more" }))
    expect(shownItems()).toHaveLength(5)
    fireEvent.click(screen.getByRole("button", { name: "show fewer" }))
    expect(shownItems()).toHaveLength(3)
  })

  // W4
  it("loads the person's dossier under the facts, once per card", async () => {
    const held = gate()
    server.dossier.mockImplementationOnce(async () => {
      await held.opened
      return new Response("<h3>Summary</h3><p>Met at Acme.</p>")
    })
    await openQueue()
    const dossier = () => must(frame().querySelector(".details .dossier-text"))
    expect(dossier().textContent).toBe("Loading…")
    held.open()
    await waitFor(() => expect(dossier().querySelector("p")?.textContent).toBe("Met at Acme."))
    expect(server.reads("/api/dossier")).toEqual(["/api/dossier?slug=casey-delta&skip=1"])

    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    await waitFor(() =>
      expect(server.reads("/api/dossier")).toEqual([
        "/api/dossier?slug=casey-delta&skip=1",
        "/api/dossier?slug=jordan-bravo&skip=1",
      ]),
    )
  })

  it("says No details found when the server has no dossier", async () => {
    server.dossier.mockImplementation(() => new Response("", { status: 404 }))
    await openQueue()
    await waitFor(() => expect(frame().querySelector(".dossier-text")?.textContent).toBe("No details found"))
  })

  it("says Could not load details when the dossier request fails", async () => {
    server.dossier.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")))
    await openQueue()
    await waitFor(() =>
      expect(frame().querySelector(".dossier-text")?.textContent).toBe("Could not load details"),
    )
  })

  // W5
  it("scrolls the person inside the card, with a cue while more sits below", async () => {
    const held = gate()
    server.dossier.mockImplementationOnce(async () => {
      await held.opened
      return new Response("<p>Met at Acme.</p>")
    })
    await openQueue()
    const box = must(frame().querySelector<HTMLElement>(".identity-scroll-shell > .identity-scroll"))
    const cue = must(frame().querySelector<HTMLButtonElement>(".identity-scroll-shell > .scroll-cue"))
    expect(cue.hidden).toBe(true)
    expect(cue.getAttribute("aria-label")).toBe("Scroll down")

    // The dossier lands and the body now runs past the fold: the cue measures again.
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 400 })
    Object.defineProperty(box, "scrollHeight", { configurable: true, value: 900 })
    held.open()
    await waitFor(() => expect(cue.hidden).toBe(false))

    const scrollBy = vi.fn()
    box.scrollBy = scrollBy
    fireEvent.click(cue)
    expect(scrollBy).toHaveBeenCalledWith({ top: 280, behavior: "auto" })

    Object.defineProperty(box, "scrollTop", { configurable: true, value: 500 })
    fireEvent.scroll(box)
    await waitFor(() => expect(cue.hidden).toBe(true))
  })

  // W6
  it("has a collapsed, optional note box", async () => {
    await openQueue()
    const why = must(frame().querySelector<HTMLDetailsElement>("details.worth-why"))
    expect(why.open).toBe(false)
    expect(why.querySelector("summary")?.textContent).toBe("Why? Give feedback (optional)")
    expect(noteBox().maxLength).toBe(2000)
    expect(noteBox().rows).toBe(2)
  })

  it.each(["Yes", "No"] as const)("sends whatever the note box holds along with a %s", async (label) => {
    await openQueue()
    fireEvent.change(noteBox(), { target: { value: "  met at the Acme offsite \n" } })
    fireEvent.click(answer(label))
    await waitFor(() =>
      expect(server.saves()).toEqual([
        {
          pub: "worth-casey",
          worth: label.toLowerCase(),
          parent_slug: "casey-delta",
          note: "met at the Acme offsite",
        },
      ]),
    )
  })

  it("starts the next card with an empty note box", async () => {
    await openQueue()
    fireEvent.change(noteBox(), { target: { value: "met at Acme" } })
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    expect(noteBox().value).toBe("")
  })
})

describe("WorthQueue: a decision", () => {
  // W7
  it("takes a Yes at once: the card fades, the counts move, the save runs behind the next card", async () => {
    const { toast, toastError, applyProgress } = await openQueue()
    const held = server.holdSave()
    expect(tabText()).toEqual(["Review3", "Yes5", "No2"])

    fireEvent.click(answer("Yes"))
    expect(frame().classList.contains("swapping")).toBe(true)
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    expect([answer("Yes").disabled, answer("No").disabled]).toEqual([true, true])
    expect(tabText()).toEqual(["Review2", "Yes6", "No2"])
    await waitFor(() =>
      expect(server.saves()).toEqual([
        { pub: "worth-casey", worth: "yes", parent_slug: "casey-delta", note: "" },
      ]),
    )

    // The save is still out: the card read ahead swaps in after the fade.
    await shows("Jordan Bravo")
    expect(frame().className).toBe("decision-card identity-card worth-card entering")
    expect([answer("Yes").disabled, answer("No").disabled]).toEqual([false, false])
    expect(tabText()).toEqual(["Review2", "Yes6", "No2"])
    expect(toast).not.toHaveBeenCalled()
    expect(applyProgress).not.toHaveBeenCalled()

    // The answer corrects the counts (the server's differ from the page's guess).
    held.open()
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Added"))
    expect(applyProgress.mock.calls).toEqual([[server.progress()]])
    expect(tabText()).toEqual(["Review2", "Yes1", "No0"])
    expect(toastError).not.toHaveBeenCalled()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Jordan Bravo")
  })

  it("takes a No the same way and says Rejected", async () => {
    const { toast } = await openQueue()
    const held = server.holdSave()
    fireEvent.click(answer("No"))
    expect(tabText()).toEqual(["Review2", "Yes5", "No3"])
    await shows("Jordan Bravo")
    held.open()
    await waitFor(() => expect(toast.mock.calls).toEqual([["Rejected"]]))
    expect(server.saves()).toEqual([
      { pub: "worth-casey", worth: "no", parent_slug: "casey-delta", note: "" },
    ])
    expect(tabText()).toEqual(["Review2", "Yes0", "No1"])
  })

  it("holds the swap until the fade is over", async () => {
    await openQueue({ fadeMs: 150 })
    fireEvent.click(answer("Yes"))
    await wait(60)
    // The next card was read long ago; only the fade keeps the old one.
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    expect(frame().classList.contains("swapping")).toBe(true)
    await shows("Jordan Bravo")
    expect(frame().classList.contains("swapping")).toBe(false)
  })

  // P0.3
  it("posts exactly once however often the buttons are pressed", async () => {
    await openQueue()
    const held = server.holdSave()
    fireEvent.click(answer("Yes"))
    fireEvent.click(answer("Yes"))
    fireEvent.click(answer("No"))
    await shows("Jordan Bravo")
    held.open()
    await waitFor(() => expect(server.piles.yes).toHaveLength(1))
    expect(server.fetch.mock.calls.filter(([url]) => url === "/worth")).toHaveLength(1)
    expect(server.saves()).toHaveLength(1)
  })

  // P0.4
  it("brings the undecided card and its counts back when the save is refused, and says why", async () => {
    const { toast, toastError, applyProgress } = await openQueue()
    const held = server.holdSave(() => refusal("review store is locked"))
    fireEvent.change(noteBox(), { target: { value: "met at Acme" } })
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    expect(tabText()).toEqual(["Review2", "Yes6", "No2"])

    held.open()
    await shows("Casey Delta")
    expect(frame().classList.contains("swapping")).toBe(false)
    expect([answer("Yes").disabled, answer("No").disabled]).toEqual([false, false])
    expect(tabText()).toEqual(["Review3", "Yes5", "No2"])
    expect(toastError.mock.calls).toEqual([["review store is locked"]])
    expect(toast).not.toHaveBeenCalled()
    expect(applyProgress).not.toHaveBeenCalled()
    expect(server.piles.pending).toHaveLength(3)

    // The card is asked again and can be decided again.
    fireEvent.click(answer("No"))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Rejected"))
    expect(server.saves()[1]).toEqual({
      pub: "worth-casey",
      worth: "no",
      parent_slug: "casey-delta",
      note: "",
    })
  })

  it("takes back only the refused save's counts when two are out", async () => {
    await openQueue()
    const first = server.holdSave(() => refusal("review store is locked"))
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    const second = server.holdSave()
    fireEvent.click(answer("No"))
    await shows("Riley Echo")
    expect(tabText()).toEqual(["Review1", "Yes6", "No3"])

    first.open()
    await shows("Casey Delta")
    expect(tabText()).toEqual(["Review2", "Yes5", "No3"])
    second.open()
    await waitFor(() => expect(tabText()).toEqual(["Review2", "Yes0", "No1"]))
  })
})

describe("WorthQueue: reading ahead", () => {
  // W8
  it("reads the card after the one on screen, leaving out that card and every save still out", async () => {
    const { toast } = await openQueue()
    expect(server.reads(CARD)).toEqual([CARD, `${CARD}?exclude=worth-casey`])

    const casey = server.holdSave()
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    // The decision used the card read ahead: no other read went out for it.
    expect(server.reads(CARD)).toEqual([
      CARD,
      `${CARD}?exclude=worth-casey`,
      `${CARD}?exclude=worth-casey%2Cworth-jordan`,
    ])

    // A saved decision leaves the list.
    casey.open()
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Added"))
    server.holdSave()
    fireEvent.click(answer("No"))
    await shows("Riley Echo")
    expect(server.reads(CARD)[3]).toBe(`${CARD}?exclude=worth-jordan%2Cworth-riley`)
    expect(server.reads(CARD)).toHaveLength(4)
  })

  it("reads the next card at the click when none was read ahead", async () => {
    const mend = failCardReads()
    const { toastError, leaveAndReload } = await openQueue()
    // The read ahead failed and the save is refused: the card stays as it was.
    server.worth.mockImplementationOnce(() => refusal("review store is locked"))
    fireEvent.change(noteBox(), { target: { value: "met at Acme" } })
    fireEvent.click(answer("Yes"))
    await waitFor(() => expect(toastError.mock.calls).toEqual([["review store is locked"]]))
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    expect(frame().className).toBe("decision-card identity-card worth-card")
    expect(noteBox().value).toBe("met at Acme")
    expect([answer("Yes").disabled, answer("No").disabled]).toEqual([false, false])
    expect(tabText()).toEqual(["Review3", "Yes5", "No2"])
    expect(leaveAndReload).not.toHaveBeenCalled()
    expect(server.reads(CARD)).toEqual([CARD, `${CARD}?exclude=worth-casey`])

    // Nothing is read ahead now, so the next click asks for the card after this one itself.
    mend()
    server.holdSave()
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    expect(server.reads(CARD).slice(2)).toEqual([
      `${CARD}?exclude=worth-casey`,
      `${CARD}?exclude=worth-casey%2Cworth-jordan`,
    ])
  })

  // W9
  it("waits for the save and reads the screen again when the next card cannot be read", async () => {
    failCardReads()
    const { leaveAndReload, applyProgress, toast, transition } = await openQueue()
    const held = server.holdSave()
    fireEvent.click(answer("Yes"))
    await waitFor(() => expect(server.worth).toHaveBeenCalledTimes(1))
    await wait(20)
    expect(leaveAndReload).not.toHaveBeenCalled()
    expect(frame().classList.contains("swapping")).toBe(true)

    held.open()
    await waitFor(() => expect(leaveAndReload.mock.calls).toEqual([["Saved"]]))
    expect(applyProgress.mock.calls).toEqual([[server.progress()]])
    expect(tabText()).toEqual(["Review2", "Yes1", "No0"])
    expect(toast).not.toHaveBeenCalled()
    expect(transition).not.toHaveBeenCalled()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
  })

  it("does the same when the read fails on the network", async () => {
    server.card.mockImplementation((query) =>
      query.has("exclude") ? Promise.reject(new TypeError("Failed to fetch")) : server.readCard(query),
    )
    const { leaveAndReload } = await openQueue()
    fireEvent.click(answer("No"))
    await waitFor(() => expect(leaveAndReload.mock.calls).toEqual([["Saved"]]))
  })
})

describe("WorthQueue: the last card", () => {
  // W10
  it("keeps the last card's frame, then runs the stage check to Enrich when nobody is left", async () => {
    server = worthServer({ pending: [personNamed("Casey Delta")] })
    vi.stubGlobal("fetch", server.fetch)
    const { transition, toast, applyProgress } = await openQueue({
      progress: pageProgress({ worth_pending: 1 }),
    })
    const held = server.holdSave()
    fireEvent.click(answer("Yes"))
    await waitFor(() => expect(server.worth).toHaveBeenCalledTimes(1))
    await wait(20)
    // The queue answered with no card: the frame holds, faded, and takes no clicks.
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    expect(frame().classList.contains("swapping")).toBe(true)
    expect([answer("Yes").disabled, answer("No").disabled]).toEqual([true, true])
    expect(transition).not.toHaveBeenCalled()

    held.open()
    await waitFor(() => expect(transition.mock.calls).toEqual([["People Reviewed", "enrich"]]))
    expect(toast.mock.calls).toEqual([["Added"]])
    expect(applyProgress).toHaveBeenCalledWith(expect.objectContaining({ worth_pending: 0 }))
  })

  it("holds the frame without the stage check while another save is still out", async () => {
    server = worthServer({ pending: [personNamed("Casey Delta"), personNamed("Jordan Bravo")] })
    vi.stubGlobal("fetch", server.fetch)
    const { transition, toast } = await openQueue({ progress: pageProgress({ worth_pending: 2 }) })
    const casey = server.holdSave()
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    fireEvent.click(answer("No"))
    await waitFor(() => expect(toast.mock.calls).toEqual([["Rejected"]]))
    // Jordan's answer still counts Casey as pending.
    expect(transition).not.toHaveBeenCalled()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Jordan Bravo")
    expect(frame().classList.contains("swapping")).toBe(true)

    casey.open()
    await waitFor(() => expect(transition.mock.calls).toEqual([["People Reviewed", "enrich"]]))
  })

  it("does nothing more once the stage has left the screen", async () => {
    server = worthServer({ pending: [personNamed("Casey Delta")] })
    vi.stubGlobal("fetch", server.fetch)
    const { transition, toast, applyProgress, unmount } = await openQueue({
      progress: pageProgress({ worth_pending: 1 }),
    })
    const held = server.holdSave()
    fireEvent.click(answer("Yes"))
    await waitFor(() => expect(server.worth).toHaveBeenCalledTimes(1))
    unmount()
    held.open()
    await waitFor(() => expect(server.piles.yes).toHaveLength(1))
    await wait(20)
    expect(transition).not.toHaveBeenCalled()
    expect(toast).not.toHaveBeenCalled()
    expect(applyProgress).not.toHaveBeenCalled()
  })
})

describe("WorthQueue: nobody to show", () => {
  // S8
  it("shows the synthesis handoff when the queue is empty because synthesis has not run", async () => {
    server = worthServer({ pending: [], synthesizePending: true })
    vi.stubGlobal("fetch", server.fetch)
    const { container } = renderWorth("review", server, { progress: pageProgress({ worth_pending: 0 }) })
    await screen.findByRole("heading", { name: "Synthesis has not run" })
    expect(screen.getByText("bin/deep-context dry")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy()
    expect(container.querySelector(".worth-panel > .empty-state")).toBeTruthy()
    expect(container.querySelector(".worth-card")).toBeNull()
    expect(screen.queryByRole("searchbox")).toBeNull()
    expect(server.reads(CARD)).toEqual([CARD])
  })

  it("swaps the synthesis handoff in when a decision empties the queue into it", async () => {
    server = worthServer({ pending: [personNamed("Casey Delta")], synthesizePending: true })
    vi.stubGlobal("fetch", server.fetch)
    await openQueue({ progress: pageProgress({ worth_pending: 1 }) })
    fireEvent.click(answer("Yes"))
    await screen.findByRole("heading", { name: "Synthesis has not run" })
    expect(document.querySelector(".worth-card")).toBeNull()
  })

  it("leaves the panel empty when the first read has no card", async () => {
    server = worthServer({ pending: [] })
    vi.stubGlobal("fetch", server.fetch)
    const { container } = renderWorth("review", server, { progress: PAGE })
    await waitFor(() => expect(server.card).toHaveBeenCalledTimes(1))
    await wait(20)
    expect(must(container.querySelector(".worth-panel")).childElementCount).toBe(0)
  })

  it("says so when the first card cannot be read", async () => {
    server.card.mockImplementationOnce(() => errorResponse("review store is locked", 500))
    renderWorth("review", server, { progress: PAGE })
    await screen.findByRole("heading", { name: "Could not load the review" })
    expect(screen.getByText("review store is locked")).toBeTruthy()
  })
})

// W11
describe("WorthQueue: the typeahead", () => {
  it("shows the search box only when people are pending, and reads their names once", async () => {
    await openQueue()
    await searchBox()
    await search("e")
    expect(listed()).toEqual(["Casey Delta", "Riley Echo"])
    expect(server.reads(NAMES)).toEqual([NAMES])
  })

  it("swaps a picked person's card in after the fade", async () => {
    await openQueue({ fadeMs: 150 })
    const input = await search("ril")
    fireEvent.mouseDown(screen.getByRole("option", { name: "Riley Echo" }))
    expect(input.value).toBe("")
    // The card stays until the picked one is read; then it fades before the swap.
    expect(frame().classList.contains("swapping")).toBe(false)
    await waitFor(() => expect(server.reads(CARD)).toContain(`${CARD}?pick=worth-riley`))
    await wait(60)
    expect(frame().classList.contains("swapping")).toBe(true)
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    await shows("Riley Echo")
    expect(frame().className).toBe("decision-card identity-card worth-card entering")
    // The card after the picked one is read ahead, as after any swap.
    expect(server.reads(CARD).slice(2)).toEqual([`${CARD}?pick=worth-riley`, `${CARD}?exclude=worth-riley`])
    expect(server.reads(NAMES)).toEqual([NAMES])
    expect(server.worth).not.toHaveBeenCalled()
  })

  it("picks the highlighted person on Enter", async () => {
    await openQueue()
    const input = await search("e")
    fireEvent.keyDown(input, { key: "ArrowDown" })
    fireEvent.keyDown(input, { key: "Enter" })
    await shows("Riley Echo")
  })

  it("says Already decided for a person who is no longer pending, and drops the name", async () => {
    const { toast, toastError } = await openQueue()
    // Decided elsewhere since the names were read.
    server.piles.pending = server.piles.pending.filter((person) => person.name !== "Riley Echo")
    await search("ril")
    fireEvent.mouseDown(screen.getByRole("option", { name: "Riley Echo" }))
    await waitFor(() => expect(toast.mock.calls).toEqual([["Already decided"]]))
    expect(toastError).not.toHaveBeenCalled()
    expect(server.reads(CARD)[2]).toBe(`${CARD}?pick=worth-riley`)
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    expect(frame().classList.contains("swapping")).toBe(false)
    await search("ril")
    expect(listed()).toEqual(["No matches"])
  })

  it("says Already decided, asking nothing, for a person whose save is still out", async () => {
    const { toast } = await openQueue()
    server.holdSave()
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    const reads = server.reads(CARD).length
    await search("cas")
    fireEvent.mouseDown(screen.getByRole("option", { name: "Casey Delta" }))
    expect(toast.mock.calls).toEqual([["Already decided"]])
    expect(server.reads(CARD)).toHaveLength(reads)
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Jordan Bravo")
    await search("cas")
    expect(listed()).toEqual(["No matches"])
  })

  it.each([
    ["the network fails", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["the server fails", () => errorResponse("boom", 500)],
  ])("says Could not load card when %s, and keeps the card and the name", async (_when, failing) => {
    const { toast, toastError } = await openQueue()
    server.card.mockImplementationOnce(failing)
    await search("ril")
    fireEvent.mouseDown(screen.getByRole("option", { name: "Riley Echo" }))
    await waitFor(() => expect(toastError.mock.calls).toEqual([["Could not load card"]]))
    expect(toast).not.toHaveBeenCalled()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
    expect(frame().classList.contains("swapping")).toBe(false)
    await search("ril")
    expect(listed()).toEqual(["Riley Echo"])
  })

  it("drops a person's name once their decision is saved, and keeps it when the save is refused", async () => {
    const { toast, toastError } = await openQueue()
    fireEvent.click(answer("Yes"))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Added"))
    await search("cas")
    expect(listed()).toEqual(["No matches"])

    server.worth.mockImplementationOnce(() => refusal("review store is locked"))
    fireEvent.click(answer("Yes"))
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("review store is locked"))
    await search("jor")
    expect(listed()).toEqual(["Jordan Bravo"])
  })

  it("says so when the names cannot be read, and shows no search box", async () => {
    server.pending.mockImplementationOnce(() => errorResponse("review store is locked", 500))
    const { toastError } = await openQueue()
    await waitFor(() => expect(toastError.mock.calls).toEqual([["review store is locked"]]))
    expect(screen.queryByRole("searchbox")).toBeNull()
  })
})

// W12
describe("WorthQueue: the debug carousel", () => {
  it("has no carousel without debug=1", async () => {
    const { container } = await openQueue()
    expect(container.querySelector(".carousel-shell")).toBeNull()
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull()
  })

  it("opens on the URL's position and browses the queue without writing", async () => {
    const { container } = await openQueue({ debug: true, index: 1 }, "Jordan Bravo")
    expect(server.reads(CARD)[0]).toBe(`${CARD}?index=1&debug=1`)
    const shell = must(container.querySelector(".worth-panel > .carousel-shell"))
    expect([...shell.children].map((child) => child.className)).toEqual([
      "carousel-nav carousel-prev",
      "carousel-nav carousel-next",
      "decision-card identity-card worth-card",
    ])

    fireEvent.click(screen.getByRole("button", { name: "Next" }))
    await shows("Riley Echo")
    fireEvent.click(screen.getByRole("button", { name: "Next" }))
    await shows("Casey Delta")
    fireEvent.click(screen.getByRole("button", { name: "Previous" }))
    await shows("Riley Echo")

    const browsed = server.reads(CARD).filter((url) => url.includes("debug=1"))
    expect(browsed).toEqual([
      `${CARD}?index=1&debug=1`,
      `${CARD}?index=2&debug=1`,
      `${CARD}?debug=1`,
      `${CARD}?index=2&debug=1`,
    ])
    expect(server.worth).not.toHaveBeenCalled()
    expect(container.querySelector(".carousel-shell")).toBeTruthy()
  })

  it("says Could not load card when a position cannot be read, and keeps the card", async () => {
    const { toastError } = await openQueue({ debug: true }, "Casey Delta")
    server.card.mockImplementationOnce(() => errorResponse("boom", 500))
    fireEvent.click(screen.getByRole("button", { name: "Next" }))
    await waitFor(() => expect(toastError.mock.calls).toEqual([["Could not load card"]]))
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Casey Delta")
  })

  it("moves on to the plain next card after a decision, as the old page did", async () => {
    const { container, toast } = await openQueue({ debug: true }, "Casey Delta")
    fireEvent.click(answer("Yes"))
    await shows("Jordan Bravo")
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Added"))
    expect(container.querySelector(".carousel-shell")).toBeNull()
    expect(frame().className).toBe("decision-card identity-card worth-card")
  })
})
