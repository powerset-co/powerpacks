import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import {
  decideResult,
  errorResponse,
  jsonResponse,
  linkedinCard,
  linkedinFinished,
  motionMedia,
  queuePosition,
  reviewCandidate,
  syntheticCandidate,
} from "@/testing/review-fixture"
import type { LinkedinCardPayload } from "@/types/review"

import {
  caseyCard,
  gate,
  refusal,
  renderStage,
  reviewServer,
  severalCard,
  spyReview,
} from "./linkedin-fixture"

const CARD = "/api/review/linkedin-card"
const DECIDE = "/api/review/decide"
const RETARGET = "/retarget"
const QUEUED = "Queued for re-research — moving on"

const server = reviewServer()

beforeEach(() => {
  server.reset()
  vi.stubGlobal("fetch", server.fetch)
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

const article = () => screen.getByRole("article")
const name = () => screen.getByRole("heading", { level: 2 }).textContent
const button = (label: string) => screen.getByRole<HTMLButtonElement>("button", { name: label })
const guidanceBox = () => must(document.querySelector<HTMLDetailsElement>("details.retarget-guidance"))
const guidanceField = () => must(guidanceBox().querySelector("textarea"))
const fading = () => article().classList.contains("swapping")
/** The card's own buttons still taking a press (the scroll cue and "show more" write nothing). */
const live = () => [...article().querySelectorAll("button:not(.scroll-cue, .show-more):not(:disabled)")]
const labels = (selector: string) =>
  [...document.querySelectorAll(`${selector} button`)].map((item) => item.textContent)

/** Opens the stage on `payload` and waits for it to draw. */
async function open(payload: LinkedinCardPayload = linkedinCard(), review = spyReview()) {
  server.answer(`GET ${CARD}`, payload)
  const view = renderStage(review)
  await screen.findByRole("heading", { level: 2 })
  return view
}

function writeGuidance(text: string) {
  fireEvent.click(screen.getByRole("button", { name: /^(No|None of these)$/ }))
  fireEvent.change(guidanceField(), { target: { value: text } })
}

describe("LinkedinStage: the card", () => {
  it("draws one candidate as the person card under the question (L1)", async () => {
    const { container } = await open()
    expect(server.gets(CARD)).toEqual([CARD])
    expect(article().className).toBe("decision-card identity-card")
    expect(container.querySelector(".linkedin-stage > .linkedin-panel > article")).toBe(article())
    expect(name()).toBe("Jordan Bravo")
    expect(screen.getByRole("link", { name: "View LinkedIn" }).getAttribute("href")).toBe(
      "https://www.linkedin.com/in/jordan-bravo",
    )
    expect([...container.querySelectorAll(".details dt")].map((term) => term.textContent)).toEqual([
      "Contact",
      "Summary",
      "Location",
      "Work",
      "Education",
    ])
    expect(container.querySelector(".question")?.textContent).toBe("Is this the right profile? Or Skip?")
    expect(labels(".question")).toEqual(["Skip"])
    expect(labels(".binary-actions")).toEqual(["No", "Use this profile"])
    expect(button("Use this profile").className).toBe("button button-primary")
    expect(container.querySelector(".linkedin-options")).toBeNull()
    await waitFor(() => expect(container.querySelector(".dossier-text p")?.textContent).toBe("Met at Acme."))
    expect(server.gets("/api/dossier")).toEqual(["/api/dossier?slug=jordan-bravo&skip=1"])
  })

  it("draws several candidates as the person alone and one option each (L2)", async () => {
    const candidates = [
      reviewCandidate(),
      syntheticCandidate({ name: "Jordan A. Bravo" }),
      reviewCandidate({ row_key: "jordan-bravo-3", url: "" }),
    ]
    const { container } = await open(severalCard(candidates))
    expect(article().className).toBe("decision-card identity-card identity-card-multi")
    expect(name()).toBe("Jordan Bravo")
    const header = must(container.querySelector(".identity-scroll"))
    expect(header.querySelector(":scope > .profile-card .linkedin-label")).toBeNull()
    expect([...header.querySelectorAll(":scope > .details dt")].map((term) => term.textContent)).toEqual([
      "Contact",
    ])
    await waitFor(() => expect(header.querySelector(":scope > .details .dossier-text p")).toBeTruthy())
    expect(container.querySelector(".linkedin-options-intro")?.textContent).toBe(
      "We found more than one possible profile — pick the right one.",
    )
    const options = [...container.querySelectorAll(".linkedin-options > li")]
    expect(options.map((option) => option.className)).toEqual([
      "linkedin-option option-linkedin",
      "linkedin-option option-synthetic",
      "linkedin-option option-linkedin",
    ])
    expect(options.map((option) => option.querySelector("h3")?.textContent)).toEqual([
      "Jordan Bravo",
      "Jordan A. Bravo",
      "Jordan Bravo",
    ])
    expect(labels(".linkedin-option")).toEqual(["Use this profile", "Use this profile", "Use this profile"])
    expect(container.querySelector(".question")).toBeNull()
    expect(labels(".identity-decision > .binary-actions")).toEqual(["None of these", "Skip"])
  })

  it("says so when the first card cannot be read", async () => {
    server.answer(`GET ${CARD}`, errorResponse("review store is locked", 500))
    renderStage()
    expect(await screen.findByRole("heading", { name: "Could not load the review" })).toBeTruthy()
    expect(screen.getByText("review store is locked")).toBeTruthy()
  })
})

describe("LinkedinStage: a decision", () => {
  it("saves Use this profile once, locks and fades the card, then shows the answer's next card (L3, L9, P0.3, P0.5)", async () => {
    const held = gate()
    server.answer(`POST ${DECIDE}`, () => held.answer)
    const { review } = await open()

    fireEvent.click(button("Use this profile"))
    fireEvent.click(button("Use this profile"))
    expect(server.posts(DECIDE)).toEqual([
      { pub: "jordan-bravo-1", decision: "keep", parent_slug: "jordan-bravo" },
    ])
    expect(fading()).toBe(true)
    expect(live()).toEqual([])
    expect(name()).toBe("Jordan Bravo")

    held.open(jsonResponse(decideResult({ next: caseyCard() })))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(article().className).toBe("decision-card identity-card entering")
    expect(live().length).toBeGreaterThan(0)
    expect(review.applyProgress).toHaveBeenCalledWith({ linkedin_pending: 3 })
    expect(review.toast).toHaveBeenCalledExactlyOnceWith("Saved")
    expect(review.transition).not.toHaveBeenCalled()
    // The next card came with the answer: the queue was read once, when the stage opened.
    expect(server.gets(CARD)).toEqual([CARD])
    expect(server.posts(DECIDE)).toHaveLength(1)
  })

  it("keeps the picked option's own candidate (L3)", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    const { review } = await open(severalCard([reviewCandidate(), syntheticCandidate()]))
    fireEvent.click(must(screen.getAllByRole("button", { name: "Use this profile" })[1]))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(server.posts(DECIDE)).toEqual([
      { pub: "jordan-bravo-research", decision: "keep", parent_slug: "jordan-bravo" },
    ])
    expect(review.toast).toHaveBeenCalledExactlyOnceWith("Saved")
  })

  it("skips from the question's link: detach, once (L4, P0.3)", async () => {
    const held = gate()
    server.answer(`POST ${DECIDE}`, () => held.answer)
    const { review } = await open()
    fireEvent.click(button("Skip"))
    fireEvent.click(button("Skip"))
    expect(server.posts(DECIDE)).toEqual([
      { pub: "jordan-bravo-1", decision: "detach", parent_slug: "jordan-bravo" },
    ])
    expect(live()).toEqual([])
    held.open(jsonResponse(decideResult({ action: "detach", next: caseyCard() })))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(review.toast).toHaveBeenCalledExactlyOnceWith("Skipped")
  })

  it("skips a several-candidate card from its button: detach for the first candidate (L4)", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ action: "detach", next: caseyCard() }))
    const { review } = await open(severalCard([reviewCandidate(), syntheticCandidate()]))
    fireEvent.click(button("Skip"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(server.posts(DECIDE)).toEqual([
      { pub: "jordan-bravo-1", decision: "detach", parent_slug: "jordan-bravo" },
    ])
    expect(review.toast).toHaveBeenCalledExactlyOnceWith("Skipped")
  })

  it("waits out the fade before the next card, even when the save is faster (L9)", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    await open(linkedinCard(), spyReview({ fadeMs: 170 }))
    vi.useFakeTimers()
    fireEvent.click(button("Use this profile"))
    await act(() => vi.advanceTimersByTimeAsync(169))
    expect([name(), fading()]).toEqual(["Jordan Bravo", true])
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect([name(), fading()]).toEqual(["Casey Delta", false])
  })

  it("restores the undecided card and says why when the save fails (P0.4)", async () => {
    server.answer(`POST ${DECIDE}`, errorResponse("stale or mismatched person card", 400))
    const { review } = await open()
    fireEvent.click(button("Use this profile"))
    await waitFor(() =>
      expect(review.toastError).toHaveBeenCalledExactlyOnceWith("stale or mismatched person card"),
    )
    expect([name(), fading()]).toEqual(["Jordan Bravo", false])
    expect(button("Use this profile").disabled).toBe(false)
    expect(button("Skip").disabled).toBe(false)
    expect(review.applyProgress).not.toHaveBeenCalled()
    expect(review.toast).not.toHaveBeenCalled()
    expect(review.transition).not.toHaveBeenCalled()

    // Nothing was dropped: the same card takes the decision again.
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    fireEvent.click(button("Use this profile"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(server.posts(DECIDE)).toHaveLength(2)
  })

  it("restores the card when the save never gets an answer (P0.4)", async () => {
    server.answer(`POST ${DECIDE}`, () => Promise.reject(new Error("Failed to fetch")))
    const { review } = await open()
    fireEvent.click(button("Skip"))
    await waitFor(() => expect(review.toastError).toHaveBeenCalledExactlyOnceWith("Failed to fetch"))
    expect([name(), fading()]).toEqual(["Jordan Bravo", false])
  })

  it("runs the wordless stage check after the last decision (L10)", async () => {
    const next = linkedinCard({
      card: null,
      finished: linkedinFinished(),
      pending: 0,
    })
    server.answer(`POST ${DECIDE}`, decideResult({ next }))
    const { review } = await open()
    fireEvent.click(button("Use this profile"))
    await waitFor(() => expect(review.transition).toHaveBeenCalledExactlyOnceWith("", "linkedin"))
    expect(review.applyProgress).toHaveBeenCalledWith({ linkedin_pending: 0 })
    // The check replaces the stage: the card stays faded and no toast is said.
    expect([name(), fading()]).toEqual(["Jordan Bravo", true])
    expect(review.toast).not.toHaveBeenCalled()
    expect(server.posts("/complete")).toEqual([])
  })

  it("counts the people left above the card, and recounts on the next card", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    const { container } = await open()
    const left = () => container.querySelector(".linkedin-stage > .queue-left")?.textContent
    expect(left()).toBe("4 left")
    fireEvent.click(button("Use this profile"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(left()).toBe("3 left")
  })

  it("counts nothing once there is no card to show", async () => {
    const finished = linkedinFinished()
    const { container } = await open(linkedinCard({ card: null, finished, pending: 0 }))
    expect(container.querySelector(".queue-left")).toBeNull()
  })
})

describe("LinkedinStage: the guidance box", () => {
  it("opens from No with the caret in it (L5)", async () => {
    await open()
    expect(guidanceBox().open).toBe(false)
    expect(button("No").getAttribute("aria-expanded")).toBe("false")
    expect(guidanceBox().querySelector("summary")?.textContent).toBe(
      "Wrong person? Provide LinkedIn or re-research",
    )

    fireEvent.click(button("No"))
    expect(guidanceBox().open).toBe(true)
    expect(document.activeElement).toBe(guidanceField())
    expect(button("No").getAttribute("aria-expanded")).toBe("true")
    expect(guidanceField().placeholder).toBe(
      "Paste a LinkedIn URL to apply it directly, or describe the right person to re-research",
    )
    expect(guidanceField().maxLength).toBe(2000)
    expect(guidanceField().required).toBe(true)
    expect(labels(".retarget-form")).toEqual(["Retarget"])
    expect(server.posts(DECIDE)).toEqual([])
  })

  it("opens from None of these on a several-candidate card (L5)", async () => {
    await open(severalCard([reviewCandidate(), syntheticCandidate()]))
    fireEvent.click(button("None of these"))
    expect(guidanceBox().open).toBe(true)
    expect(document.activeElement).toBe(guidanceField())
    expect(button("None of these").getAttribute("aria-expanded")).toBe("true")
  })

  it("opens and shuts from its own summary (L5)", async () => {
    await open()
    const summary = screen.getByText("Wrong person? Provide LinkedIn or re-research")
    fireEvent.click(summary)
    await waitFor(() => expect(button("No").getAttribute("aria-expanded")).toBe("true"))
    expect(guidanceBox().open).toBe(true)
    // Only No / None of these put the caret in the box.
    expect(document.activeElement).not.toBe(guidanceField())
    fireEvent.click(summary)
    await waitFor(() => expect(button("No").getAttribute("aria-expanded")).toBe("false"))
    expect(guidanceBox().open).toBe(false)
  })

  it("applies a pasted LinkedIn URL through the free decide and never re-researches (P0.1, L6)", async () => {
    const held = gate()
    server.answer(`POST ${DECIDE}`, () => held.answer)
    const { review } = await open()
    writeGuidance("it is https://www.linkedin.com/in/jordan-b-2 not this one")
    fireEvent.click(button("Retarget"))
    fireEvent.click(button("Retarget"))
    expect(server.posts(DECIDE)).toEqual([
      {
        pub: "jordan-bravo-1",
        decision: "fix",
        parent_slug: "jordan-bravo",
        new_url: "https://www.linkedin.com/in/jordan-b-2",
      },
    ])
    expect(server.posts(RETARGET)).toEqual([])
    expect(fading()).toBe(true)
    expect(live()).toEqual([])

    held.open(jsonResponse(decideResult({ action: "fix", next: caseyCard() })))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(review.toast).toHaveBeenCalledExactlyOnceWith("Applied")
    expect(server.gets(CARD)).toEqual([CARD])
    expect(server.posts(RETARGET)).toEqual([])
    // The next person's box starts shut and empty.
    expect(guidanceBox().open).toBe(false)
    expect(guidanceField().value).toBe("")
  })

  it("applies the URL for the first candidate of a several-candidate card (P0.1)", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ action: "fix", next: caseyCard() }))
    await open(severalCard([reviewCandidate(), syntheticCandidate()]))
    writeGuidance("linkedin.com/in/jordan-b-2")
    fireEvent.click(button("Retarget"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(server.posts(DECIDE)).toEqual([
      {
        pub: "jordan-bravo-1",
        decision: "fix",
        parent_slug: "jordan-bravo",
        new_url: "linkedin.com/in/jordan-b-2",
      },
    ])
    expect(server.posts(RETARGET)).toEqual([])
  })

  it("keeps the card when the URL cannot be applied (P0.4)", async () => {
    server.answer(`POST ${DECIDE}`, errorResponse("not a LinkedIn profile URL", 400))
    const { review } = await open()
    writeGuidance("linkedin.com/in/jordan-b-2")
    fireEvent.click(button("Retarget"))
    await waitFor(() =>
      expect(review.toastError).toHaveBeenCalledExactlyOnceWith("not a LinkedIn profile URL"),
    )
    expect([name(), fading()]).toEqual(["Jordan Bravo", false])
    expect(button("Retarget").disabled).toBe(false)
    expect(server.posts(RETARGET)).toEqual([])
  })

  it("sends a description to the paid re-research once, never to decide, then moves on (P0.1, L7)", async () => {
    const held = gate()
    server.answer(`POST ${RETARGET}`, () => held.answer)
    const { review } = await open()
    writeGuidance("  the founder of Example Labs, not the Acme engineer ")
    fireEvent.click(button("Retarget"))
    fireEvent.click(button("Retarget"))
    expect(server.posts(RETARGET)).toEqual([
      {
        pub: "jordan-bravo-1",
        parent_slug: "jordan-bravo",
        guidance: "the founder of Example Labs, not the Acme engineer",
      },
    ])
    expect(server.posts(DECIDE)).toEqual([])
    // The card takes no other decision once its re-research is asked for; it has not started to leave.
    expect(button("Retarget").disabled).toBe(true)
    expect(button("Use this profile").disabled).toBe(true)
    expect(fading()).toBe(false)
    expect(review.toast).not.toHaveBeenCalled()

    server.answer(`GET ${CARD}`, caseyCard())
    held.open(jsonResponse({ ok: true }))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(review.toast).toHaveBeenCalledExactlyOnceWith(QUEUED)
    expect(server.gets(CARD)).toEqual([CARD, `${CARD}?exclude=jordan-bravo`])
    expect(server.posts(RETARGET)).toHaveLength(1)
    expect(server.posts(DECIDE)).toEqual([])
    expect(review.applyProgress).not.toHaveBeenCalled()
    expect(article().classList.contains("entering")).toBe(true)
  })

  it("fades the card for the fade's length while the next one is read (L7)", async () => {
    server.answer(`POST ${RETARGET}`, { ok: true })
    await open(linkedinCard(), spyReview({ fadeMs: 170 }))
    writeGuidance("the founder of Example Labs")
    server.answer(`GET ${CARD}`, caseyCard())
    vi.useFakeTimers()
    fireEvent.click(button("Retarget"))
    await act(() => vi.advanceTimersByTimeAsync(169))
    expect([name(), fading()]).toEqual(["Jordan Bravo", true])
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect([name(), fading()]).toEqual(["Casey Delta", false])
  })

  it("hands Retarget back and says why when the re-research is refused (L7)", async () => {
    server.answer(`POST ${RETARGET}`, refusal("re-research is not available", 409))
    const { review } = await open()
    writeGuidance("the founder of Example Labs")
    fireEvent.click(button("Retarget"))
    await waitFor(() =>
      expect(review.toastError).toHaveBeenCalledExactlyOnceWith("re-research is not available"),
    )
    expect(button("Retarget").disabled).toBe(false)
    expect([name(), fading()]).toEqual(["Jordan Bravo", false])
    expect(guidanceField().value).toBe("the founder of Example Labs")
    expect(review.toast).not.toHaveBeenCalled()
    expect(server.gets(CARD)).toEqual([CARD])
    expect(server.posts(DECIDE)).toEqual([])
  })

  it("sends nothing for an empty or blank box (P0.1)", async () => {
    await open()
    fireEvent.click(button("No"))
    fireEvent.click(button("Retarget"))
    fireEvent.change(guidanceField(), { target: { value: "   " } })
    fireEvent.click(button("Retarget"))
    fireEvent.submit(must(guidanceBox().querySelector("form")))
    await act(() => Promise.resolve())
    expect(server.posts(RETARGET)).toEqual([])
    expect(server.posts(DECIDE)).toEqual([])
    expect(button("Retarget").disabled).toBe(false)
  })

  it("reads the screen again when the next card cannot be read, and the card takes no decision", async () => {
    server.answer(`POST ${RETARGET}`, { ok: true })
    const { review } = await open()
    writeGuidance("the founder of Example Labs")
    server.answer(`GET ${CARD}`, errorResponse("review store is locked", 500))
    fireEvent.click(button("Retarget"))
    await waitFor(() => expect(review.leaveAndReload).toHaveBeenCalledExactlyOnceWith(QUEUED))
    // The re-research may still save this person's No: the card cannot be decided meanwhile.
    expect(live()).toEqual([])
    expect(server.posts(RETARGET)).toHaveLength(1)
  })

  it("registers a typed draft with the page and clears it when the card leaves (X3)", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    const { review } = await open()
    expect(review.setGuidanceDraft).toHaveBeenLastCalledWith(false)
    writeGuidance("the founder")
    expect(review.setGuidanceDraft).toHaveBeenLastCalledWith(true)
    fireEvent.change(guidanceField(), { target: { value: "  " } })
    expect(review.setGuidanceDraft).toHaveBeenLastCalledWith(false)
    fireEvent.change(guidanceField(), { target: { value: "the founder" } })
    expect(review.setGuidanceDraft).toHaveBeenLastCalledWith(true)

    fireEvent.click(button("Use this profile"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(review.setGuidanceDraft).toHaveBeenLastCalledWith(false)
  })
})

describe("LinkedinStage: the debug carousel (L14)", () => {
  const debugging = () => spyReview({ debug: true })
  const first = () => linkedinCard({ queue: queuePosition({ index: 0, total: 3 }) })

  it("reads the queue position the URL names", async () => {
    await open(linkedinCard({ queue: queuePosition({ index: 2 }) }), spyReview({ debug: true, index: 2 }))
    expect(server.gets(CARD)).toEqual([`${CARD}?index=2&debug=1`])
    expect(button("Previous")).toBeTruthy()
    expect(button("Next")).toBeTruthy()
  })

  it("has no carousel outside debug", async () => {
    await open()
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull()
  })

  it("browses by position without writing, and without the swap", async () => {
    await open(first(), debugging())
    server.answer(`GET ${CARD}`, caseyCard({ queue: queuePosition({ index: 1, total: 3 }) }))
    fireEvent.click(button("Next"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(article().className).toBe("decision-card identity-card")

    server.answer(`GET ${CARD}`, first())
    fireEvent.click(button("Previous"))
    await waitFor(() => expect(name()).toBe("Jordan Bravo"))
    fireEvent.click(button("Previous"))
    await waitFor(() => expect(server.gets(CARD)).toHaveLength(4))
    expect(server.gets(CARD)).toEqual([
      `${CARD}?debug=1`,
      `${CARD}?index=1&debug=1`,
      `${CARD}?debug=1`,
      `${CARD}?index=2&debug=1`,
    ])
    expect(server.posts(DECIDE)).toEqual([])
    expect(server.posts(RETARGET)).toEqual([])
  })

  it("keeps the card and says so when a position cannot be read", async () => {
    const { review } = await open(first(), debugging())
    server.answer(`GET ${CARD}`, errorResponse("review store is locked", 500))
    fireEvent.click(button("Next"))
    await waitFor(() => expect(review.toastError).toHaveBeenCalledExactlyOnceWith("Could not load card"))
    expect(name()).toBe("Jordan Bravo")
    expect(button("Next")).toBeTruthy()
  })

  it("leaves the carousel on a decision: the answer's card is the plain queue's", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    await open(first(), debugging())
    fireEvent.click(button("Use this profile"))
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull()
    expect(article().className).toBe("decision-card identity-card")
  })

  it("hides the arrows while the card's re-research request is out, so no other card can be decided", async () => {
    const held = gate()
    server.answer(`POST ${RETARGET}`, () => held.answer)
    await open(first(), debugging())
    writeGuidance("the founder of Example Labs")
    fireEvent.click(button("Retarget"))
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Previous" })).toBeNull()
  })

  it("reads the screen again when a queued re-research cannot read the next card", async () => {
    server.answer(`POST ${RETARGET}`, { ok: true })
    const { review } = await open(first(), debugging())
    writeGuidance("the founder of Example Labs")
    server.answer(`GET ${CARD}`, errorResponse("review store is locked", 500))
    fireEvent.click(button("Retarget"))
    await waitFor(() => expect(review.leaveAndReload).toHaveBeenCalledExactlyOnceWith(QUEUED))
    expect(fading()).toBe(false)
    expect(button("Retarget").disabled).toBe(true)
  })
})

describe("LinkedinStage: the person menu and feedback (L13)", () => {
  const items = () => must(document.querySelector<HTMLElement>(".person-menu-items"))
  const popovers = () => document.querySelectorAll(".feedback-popover")
  /** Lets the popover start listening for outside clicks (it waits out its opening click). */
  const settle = () => act(() => new Promise<void>((resolve) => window.setTimeout(resolve, 0)))

  it("opens and shuts the menu from its toggle", async () => {
    await open()
    expect(article().firstElementChild?.className).toBe("card-menu person-menu")
    expect(button("More actions").textContent).toBe("⋯")
    expect(items().hidden).toBe(true)
    fireEvent.click(button("More actions"))
    expect(items().hidden).toBe(false)
    expect(labels(".person-menu-items")).toEqual(["Leave feedback"])
    fireEvent.click(button("More actions"))
    expect(items().hidden).toBe(true)
  })

  it("shuts an open menu on a click anywhere else", async () => {
    await open()
    fireEvent.click(button("More actions"))
    fireEvent.click(screen.getByRole("heading", { level: 2 }))
    expect(items().hidden).toBe(true)
    expect(popovers()).toHaveLength(0)
  })

  it("opens the feedback popover in the card, on the person, and shuts the menu", async () => {
    await open()
    fireEvent.click(button("More actions"))
    fireEvent.click(button("Leave feedback"))
    expect(items().hidden).toBe(true)
    expect(popovers()).toHaveLength(1)
    expect(article().lastElementChild?.className).toBe("feedback-popover")
    expect(document.querySelector(".feedback-context")?.textContent).toBe(
      "Feedback on Jordan Bravo — wrong or missing info?",
    )
    await settle()
    expect(popovers()).toHaveLength(1)
  })

  it("files the feedback on the card's first candidate", async () => {
    server.answer("POST /feedback", { ok: true })
    await open(severalCard([reviewCandidate(), syntheticCandidate()]))
    fireEvent.click(button("More actions"))
    fireEvent.click(button("Leave feedback"))
    fireEvent.change(must(document.querySelector(".feedback-popover textarea")), {
      target: { value: "Neither is right" },
    })
    fireEvent.click(button("Send feedback"))
    expect(await screen.findByText("Got it, thanks! 🙏")).toBeTruthy()
    expect(server.posts("/feedback")).toEqual([
      { pub: "jordan-bravo-1", parent_slug: "jordan-bravo", comment: "Neither is right", action: "general" },
    ])
  })

  it("keeps one popover at a time: the toggle is outside it", async () => {
    await open()
    fireEvent.click(button("More actions"))
    fireEvent.click(button("Leave feedback"))
    await settle()
    fireEvent.click(button("More actions"))
    expect(popovers()).toHaveLength(0)
    expect(items().hidden).toBe(false)
    fireEvent.click(button("Leave feedback"))
    expect(popovers()).toHaveLength(1)
  })

  it("shuts the popover on a decision and starts the next card without one", async () => {
    server.answer(`POST ${DECIDE}`, decideResult({ next: caseyCard() }))
    await open()
    fireEvent.click(button("More actions"))
    fireEvent.click(button("Leave feedback"))
    await settle()
    fireEvent.click(button("Use this profile"))
    expect(popovers()).toHaveLength(0)
    await waitFor(() => expect(name()).toBe("Casey Delta"))
    expect(popovers()).toHaveLength(0)
    expect(items().hidden).toBe(true)
  })
})
