import { act, cleanup, screen } from "@testing-library/react"
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
  it("says the review is complete and the index is updating, and presses nothing (L11)", async () => {
    finishedQueue()
    const { container } = await open()
    expect(container.querySelector(".linkedin-panel > .empty-state")).toBeTruthy()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("LinkedIn Profiles Checked")
    expect(container.querySelector(".empty-mark")).toBeNull()
    expect(lines(container)).toEqual([
      "Review complete — updating your index.",
      "You can search your confirmed contacts while you wait.",
    ])
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull()
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([])
  })

  it("shows the synthesis handoff instead when synthesis has not run (S8)", async () => {
    finishedQueue({ synthesize_pending: true })
    await open()
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Synthesis has not run")
    expect(screen.getByText("bin/deep-context-v2 run")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Finish" })).toBeNull()
    await act(() => Promise.resolve())
    expect(server.posts(COMPLETE)).toEqual([])
  })
})
