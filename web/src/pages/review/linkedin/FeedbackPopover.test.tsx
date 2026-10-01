import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { useRef } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { jsonResponse } from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"

import { FeedbackPopover } from "./FeedbackPopover"
import { gate, refusal, reviewServer, spyReview } from "./linkedin-fixture"

const FEEDBACK = "/feedback"
const SIGN_IN = "/auth/login"
const NOT_SIGNED_IN = "not signed in to Powerset; run $powerset login first"
const needsAuth = () => jsonResponse({ status: "needs_auth", error: NOT_SIGNED_IN }, 401)

const server = reviewServer()

beforeEach(() => {
  server.reset()
  vi.stubGlobal("fetch", server.fetch)
  vi.useFakeTimers()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// The popover in a card, under the person menu's box, with something else on the page.
function Card({ onClose }: { onClose: () => void }) {
  const menu = useRef<HTMLDivElement>(null)
  return (
    <>
      <article className="identity-card">
        <div ref={menu} data-testid="menu">
          <button type="button">More actions</button>
        </div>
        <FeedbackPopover
          anchor={menu}
          place={{ top: 44, right: 11 }}
          context="Feedback on Jordan Bravo — wrong or missing info?"
          pub="jordan-bravo-1"
          slug="jordan-bravo"
          onClose={onClose}
        />
      </article>
      <p>Elsewhere</p>
    </>
  )
}

function renderPopover() {
  const review = spyReview()
  const onClose = vi.fn()
  render(
    <ReviewHarness review={review}>
      <Card onClose={onClose} />
    </ReviewHarness>,
  )
  return { review, onClose }
}

/** Opens the popover and lets it start listening for outside clicks. */
async function openPopover() {
  const view = renderPopover()
  await tick()
  return view
}

const tick = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms))
const popover = () => must(document.querySelector<HTMLElement>(".feedback-popover"))
const field = () => must(popover().querySelector("textarea"))
const send = () => screen.getByRole<HTMLButtonElement>("button", { name: "Send feedback" })
const skip = () => screen.getByRole<HTMLButtonElement>("button", { name: "Skip" })
const signIn = () => popover().querySelector<HTMLButtonElement>(".feedback-login")
const write = (text: string) => fireEvent.change(field(), { target: { value: text } })

describe("FeedbackPopover: writing", () => {
  it("draws the context line, the box, the hint, Skip and a send button that waits for text", () => {
    renderPopover()
    expect(popover().className).toBe("feedback-popover")
    expect([popover().style.top, popover().style.right]).toEqual(["44px", "11px"])
    expect(popover().querySelector(".feedback-context")?.textContent).toBe(
      "Feedback on Jordan Bravo — wrong or missing info?",
    )
    expect(field().placeholder).toBe('e.g. "Wrong person — this is actually Jane Smith"')
    expect([field().rows, field().maxLength]).toEqual([2, 4000])
    expect(popover().querySelector(".feedback-hint")?.textContent).toBe("↵ ⌘+Enter")
    expect(skip().className).toBe("feedback-skip")
    expect(send().className).toBe("feedback-send")
    expect(send().disabled).toBe(true)
    expect(signIn()).toBeNull()
  })

  it("puts the caret in the box 80 ms after it opens", async () => {
    renderPopover()
    await tick(79)
    expect(document.activeElement).not.toBe(field())
    await tick(1)
    expect(document.activeElement).toBe(field())
  })

  it("enables send only while there is text", async () => {
    await openPopover()
    write("   ")
    expect(send().disabled).toBe(true)
    write("Wrong person")
    expect(send().disabled).toBe(false)
    write("")
    expect(send().disabled).toBe(true)
  })

  it("grows the box with its text, up to 140px", async () => {
    await openPopover()
    Object.defineProperty(field(), "scrollHeight", { configurable: true, value: 96 })
    write("Two lines\nof feedback")
    expect(field().style.height).toBe("96px")
    Object.defineProperty(field(), "scrollHeight", { configurable: true, value: 400 })
    write("Many more lines")
    expect(field().style.height).toBe("140px")
  })
})

describe("FeedbackPopover: closing", () => {
  it("closes on Skip", async () => {
    const { onClose } = await openPopover()
    fireEvent.click(skip())
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("closes on Escape in the box", async () => {
    const { onClose } = await openPopover()
    fireEvent.keyDown(field(), { key: "Escape" })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("closes on a click outside, and not on one inside or on the menu's own box", async () => {
    const { onClose } = await openPopover()
    fireEvent.click(field())
    fireEvent.click(must(popover().querySelector(".feedback-hint")))
    fireEvent.click(screen.getByTestId("menu"))
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText("Elsewhere"))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("counts the menu's toggle as outside", async () => {
    const { onClose } = await openPopover()
    fireEvent.click(screen.getByRole("button", { name: "More actions" }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("does not take the click that opened it for a click outside", () => {
    const { onClose } = renderPopover()
    fireEvent.click(screen.getByText("Elsewhere"))
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe("FeedbackPopover: sending", () => {
  it("sends the trimmed text once, thanks for 900 ms, then closes", async () => {
    const held = gate()
    server.answer(`POST ${FEEDBACK}`, () => held.answer)
    const { review, onClose } = await openPopover()
    write("  Wrong person, this is the Acme engineer  ")
    fireEvent.click(send())
    expect(server.posts(FEEDBACK)).toEqual([
      {
        pub: "jordan-bravo-1",
        parent_slug: "jordan-bravo",
        comment: "Wrong person, this is the Acme engineer",
        action: "general",
      },
    ])
    expect([send().disabled, skip().disabled]).toEqual([true, true])
    fireEvent.keyDown(field(), { key: "Enter", metaKey: true })
    expect(server.posts(FEEDBACK)).toHaveLength(1)

    held.open(jsonResponse({ ok: true }))
    await tick()
    expect(popover().className).toBe("feedback-popover feedback-done")
    expect(popover().textContent).toBe("Got it, thanks! 🙏")
    expect(popover().querySelector(".feedback-done-badge svg")).toBeTruthy()
    expect(popover().querySelector("textarea")).toBeNull()
    expect(popover().style.top).toBe("44px")

    // The thanks beat closes itself: a click outside does not hurry it.
    fireEvent.click(screen.getByText("Elsewhere"))
    await tick(899)
    expect(onClose).not.toHaveBeenCalled()
    await tick(1)
    expect(onClose).toHaveBeenCalledOnce()
    expect(review.toastError).not.toHaveBeenCalled()
  })

  it("sends on ⌘+Enter and on Ctrl+Enter, not on Enter alone or with nothing typed", async () => {
    server.answer(`POST ${FEEDBACK}`, { ok: true })
    await openPopover()
    fireEvent.keyDown(field(), { key: "Enter", metaKey: true })
    write("Missing their current job")
    fireEvent.keyDown(field(), { key: "Enter" })
    expect(server.posts(FEEDBACK)).toEqual([])

    fireEvent.keyDown(field(), { key: "Enter", metaKey: true })
    await tick()
    expect(server.posts(FEEDBACK)).toHaveLength(1)
    expect(popover().className).toBe("feedback-popover feedback-done")
    cleanup()

    await openPopover()
    write("Missing their current job")
    fireEvent.keyDown(field(), { key: "Enter", ctrlKey: true })
    await tick()
    expect(server.posts(FEEDBACK)).toHaveLength(2)
  })

  it("can be closed while the send is out", async () => {
    server.answer(`POST ${FEEDBACK}`, () => gate().answer)
    const { onClose } = await openPopover()
    write("Wrong person")
    fireEvent.click(send())
    fireEvent.keyDown(field(), { key: "Escape" })
    fireEvent.click(screen.getByText("Elsewhere"))
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it("says why and hands Send back on a refusal, with no sign-in offer", async () => {
    server.answer(`POST ${FEEDBACK}`, refusal("review row not found", 404))
    const { review, onClose } = await openPopover()
    write("Wrong person")
    fireEvent.click(send())
    await tick()
    expect(review.toastError).toHaveBeenCalledExactlyOnceWith("review row not found")
    expect([send().disabled, skip().disabled]).toEqual([false, false])
    expect(field().value).toBe("Wrong person")
    expect(signIn()).toBeNull()
    expect(onClose).not.toHaveBeenCalled()

    server.answer(`POST ${FEEDBACK}`, { ok: true })
    fireEvent.click(send())
    await tick()
    expect(popover().className).toBe("feedback-popover feedback-done")
  })
})

describe("FeedbackPopover: the Powerset sign-in", () => {
  /** A send refused for want of a sign-in. */
  async function refused() {
    server.answer(`POST ${FEEDBACK}`, needsAuth())
    const view = await openPopover()
    write("Wrong person")
    fireEvent.click(send())
    await tick()
    return view
  }

  it("offers the sign-in once, with the server's words, and hands Send back", async () => {
    const { review } = await refused()
    expect(review.toastError).toHaveBeenCalledExactlyOnceWith(NOT_SIGNED_IN)
    expect(must(signIn()).textContent).toBe("Sign in to Powerset")
    expect(popover().lastElementChild).toBe(signIn())
    expect([send().disabled, skip().disabled]).toEqual([false, false])
    expect(field().value).toBe("Wrong person")

    fireEvent.click(send())
    await tick()
    expect(popover().querySelectorAll(".feedback-login")).toHaveLength(1)
    expect(server.posts(FEEDBACK)).toHaveLength(2)
  })

  it("opens the sign-in once and says to send again", async () => {
    const held = gate()
    server.answer(`POST ${SIGN_IN}`, () => held.answer)
    const { review } = await refused()
    const offer = must(signIn())
    fireEvent.click(offer)
    fireEvent.click(offer)
    expect(server.posts(SIGN_IN)).toEqual([{}])
    expect([offer.disabled, offer.textContent]).toEqual([true, "Waiting for sign-in…"])
    expect(review.toast).not.toHaveBeenCalled()

    held.open(jsonResponse({ ok: true, status: "started" }))
    await tick()
    expect(review.toast).toHaveBeenCalledExactlyOnceWith(
      "Sign-in opened in your browser — finish there, then Send again.",
    )
    expect([offer.disabled, offer.textContent]).toEqual([true, "Waiting for sign-in…"])

    server.answer(`POST ${FEEDBACK}`, { ok: true })
    fireEvent.click(send())
    await tick()
    expect(popover().textContent).toBe("Got it, thanks! 🙏")
  })

  it("hands the sign-in button back when the sign-in cannot open", async () => {
    server.answer(`POST ${SIGN_IN}`, refusal("no browser to open", 500))
    const { review } = await refused()
    const offer = must(signIn())
    fireEvent.click(offer)
    await tick()
    expect(review.toastError).toHaveBeenLastCalledWith("no browser to open")
    expect([offer.disabled, offer.textContent]).toEqual([false, "Sign in to Powerset"])
    expect(review.toast).not.toHaveBeenCalled()
  })
})
