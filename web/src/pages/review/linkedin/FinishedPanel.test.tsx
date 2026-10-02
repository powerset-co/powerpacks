import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { linkedinCard, linkedinFinished, motionMedia } from "@/testing/review-fixture"
import type { LinkedinFinished } from "@/types/review"

import { renderStage, reviewServer } from "./linkedin-fixture"

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
  const finished = linkedinFinished(overrides)
  server.answer(`GET ${CARD}`, linkedinCard({ card: null, finished, pending: 0 }))
}

async function open() {
  const view = renderStage()
  await screen.findByRole("heading", { level: 2 })
  return view
}

const lines = (container: HTMLElement) =>
  [...container.querySelectorAll(".empty-state > p")].map((line) => line.textContent)

describe("FinishedPanel", () => {
  it("shows only the title and the hand-back to Codex, and presses nothing (L11)", async () => {
    finishedQueue()
    const { container, review } = await open()
    expect(container.querySelector(".linkedin-panel > .empty-state")).toBeTruthy()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("LinkedIn Profiles Checked")
    expect(container.querySelector(".empty-mark")).toBeNull()
    expect(lines(container)).toEqual(["Review complete — go back to Codex."])
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([])
    expect(review.transition).not.toHaveBeenCalled()
  })

  it("hands back to Codex once everything is decided (L11)", async () => {
    const write = vi.fn(() => Promise.resolve())
    vi.stubGlobal("navigator", { clipboard: { writeText: write } })
    finishedQueue()
    const { container, review } = await open()
    expect(lines(container)).toEqual(["Review complete — go back to Codex."])
    expect(container.querySelector(".handoff-copy code")?.textContent).toBe("Review complete, continue")
    expect(screen.queryByRole("button", { name: "Finish" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() => expect(review.toast).toHaveBeenCalledExactlyOnceWith("Copied"))
    expect(write).toHaveBeenCalledExactlyOnceWith("Review complete, continue")
  })

  it("names the phrase to type when the clipboard refuses (L11)", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: () => Promise.reject(new Error("denied")) } })
    finishedQueue()
    const { review } = await open()
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() =>
      expect(review.toastError).toHaveBeenCalledExactlyOnceWith(
        "Copy failed — type: Review complete, continue",
      ),
    )
  })

  it("shows the synthesis handoff instead when synthesis has not run (S8)", async () => {
    finishedQueue({ synthesize_pending: true })
    await open()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Synthesis has not run")
    expect(screen.getByText("bin/deep-context dry")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Finish" })).toBeNull()
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([])
  })
})
