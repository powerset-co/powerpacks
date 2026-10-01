import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { StrictMode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { jsonResponse, linkedinCard, linkedinFinished, motionMedia } from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"
import type { LinkedinFinished } from "@/types/review"

import { gate, refusal, renderStage, reviewServer, spyReview } from "./linkedin-fixture"
import { LinkedinStage } from "./LinkedinStage"

const CARD = "/api/review/linkedin-card"
const COMPLETE = "/complete"

const server = reviewServer()

beforeEach(() => {
  server.reset()
  vi.stubGlobal("fetch", server.fetch)
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** The queue has nothing to show: the stage opens on the finished state. */
function finishedQueue(overrides: Partial<LinkedinFinished> = {}) {
  const finished = linkedinFinished({ auto_continue: false, ...overrides })
  server.answer(`GET ${CARD}`, linkedinCard({ card: null, finished, pending: 0 }))
}

async function open() {
  const view = renderStage()
  await screen.findByRole("heading", { level: 2 })
  return view
}

const finish = () => screen.getByRole<HTMLButtonElement>("button", { name: "Finish" })
const lines = (container: HTMLElement) =>
  [...container.querySelectorAll(".empty-state > p")].map((line) => line.textContent)

describe("FinishedPanel", () => {
  it("counts the saved decisions and offers Finish while people are still pending (L11)", async () => {
    finishedQueue({ linkedin_done: 6 })
    const { container } = await open()
    expect(container.querySelector(".linkedin-panel > .empty-state")).toBeTruthy()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("LinkedIn Profiles Checked")
    expect(container.querySelector(".empty-mark")).toBeNull()
    expect(lines(container)).toEqual(["6 decisions saved"])
    expect(finish().className).toBe("button button-primary")
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull()
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([])
  })

  it("says how much re-research is still running (L11)", async () => {
    finishedQueue({ retargets_in_flight: 2 })
    const { container } = await open()
    expect(lines(container)).toEqual(["6 decisions saved", "2 re-research still running"])
  })

  it("hands back to Codex once everything is decided (L11)", async () => {
    const write = vi.fn(() => Promise.resolve())
    vi.stubGlobal("navigator", { clipboard: { writeText: write } })
    finishedQueue({ linkedin_complete: true })
    const { container, review } = await open()
    expect(lines(container)).toEqual(["6 decisions saved", "Review complete — go back to Codex."])
    expect(container.querySelector(".handoff-copy code")?.textContent).toBe("Review complete, continue")
    expect(screen.queryByRole("button", { name: "Finish" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() => expect(review.toast).toHaveBeenCalledExactlyOnceWith("Copied"))
    expect(write).toHaveBeenCalledExactlyOnceWith("Review complete, continue")
  })

  it("names the phrase to type when the clipboard refuses (L11)", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: () => Promise.reject(new Error("denied")) } })
    finishedQueue({ linkedin_complete: true })
    const { review } = await open()
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() =>
      expect(review.toastError).toHaveBeenCalledExactlyOnceWith(
        "Copy failed — type: Review complete, continue",
      ),
    )
  })

  it("marks the stage complete on Finish, once, then runs the wordless stage check (L12)", async () => {
    const held = gate()
    server.answer(`POST ${COMPLETE}`, () => held.answer)
    finishedQueue()
    const { review } = await open()
    fireEvent.click(finish())
    fireEvent.click(finish())
    expect(server.posts(COMPLETE)).toEqual([{ stage: "linkedin" }])
    expect([finish().disabled, finish().getAttribute("aria-busy")]).toEqual([true, "true"])
    expect(review.setCompleting).toHaveBeenCalledExactlyOnceWith(true)
    expect(review.transition).not.toHaveBeenCalled()

    held.open(jsonResponse({ ok: true }))
    await waitFor(() => expect(review.transition).toHaveBeenCalledExactlyOnceWith("", "linkedin"))
    expect(review.toast).not.toHaveBeenCalled()
    expect(server.posts(COMPLETE)).toHaveLength(1)
  })

  it("hands Finish back and says why when the stage cannot be completed (L12)", async () => {
    server.answer(`POST ${COMPLETE}`, refusal("unknown review stage: linkedin", 409))
    finishedQueue()
    const { review } = await open()
    fireEvent.click(finish())
    await waitFor(() =>
      expect(review.toastError).toHaveBeenCalledExactlyOnceWith("unknown review stage: linkedin"),
    )
    expect([finish().disabled, finish().getAttribute("aria-busy")]).toEqual([false, null])
    expect(review.setCompleting).toHaveBeenLastCalledWith(false)
    expect(review.transition).not.toHaveBeenCalled()
  })

  it("presses Finish itself, once, when the screen opens on it and the server asks (L11)", async () => {
    server.answer(`POST ${COMPLETE}`, { ok: true })
    finishedQueue({ auto_continue: true, retargets_in_flight: 1 })
    const { review } = await open()
    await waitFor(() => expect(review.transition).toHaveBeenCalledExactlyOnceWith("", "linkedin"))
    expect(server.posts(COMPLETE)).toEqual([{ stage: "linkedin" }])
    expect(review.setCompleting).toHaveBeenCalledExactlyOnceWith(true)
  })

  it("still presses once when React runs its effects twice", async () => {
    server.answer(`POST ${COMPLETE}`, { ok: true })
    finishedQueue({ auto_continue: true })
    const review = spyReview()
    render(
      <StrictMode>
        <ReviewHarness review={review}>
          <LinkedinStage />
        </ReviewHarness>
      </StrictMode>,
    )
    await waitFor(() => expect(review.transition).toHaveBeenCalled())
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([{ stage: "linkedin" }])
  })

  it("shows the synthesis handoff instead when synthesis has not run (S8)", async () => {
    server.answer(`POST ${COMPLETE}`, { ok: true })
    finishedQueue({ synthesize_pending: true, auto_continue: true })
    await open()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Synthesis has not run")
    expect(screen.getByText("bin/deep-context dry")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Finish" })).toBeNull()
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([])
  })
})
