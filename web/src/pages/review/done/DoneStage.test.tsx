import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import { fakeReview } from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"

import { DoneStage } from "./DoneStage"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderDone(checked: number, rejected: number, review = fakeReview()) {
  return render(
    <ReviewHarness review={review}>
      <DoneStage checked={checked} rejected={rejected} />
    </ReviewHarness>,
  )
}

describe("DoneStage (D1)", () => {
  it("draws the check, All set, the two counts and the go-back handoff, in that order", () => {
    const { container } = renderDone(11, 4)
    const panel = must(container.firstElementChild, "the panel")
    expect(panel.className).toBe("empty-state done")
    expect([...panel.children].map((child) => child.className || child.tagName)).toEqual([
      "empty-mark",
      "H2",
      "P",
      "handoff-note",
      "handoff-copy",
    ])
    expect(panel.querySelector(".empty-mark")?.textContent).toBe("✓")
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("All set")
    expect(panel.querySelector("p")?.textContent).toBe("11 identities checked · 4 rejected")
    expect(panel.querySelector(".handoff-note")?.textContent).toBe("Review complete — go back to Codex.")
    expect(panel.querySelector(".handoff-copy code")?.textContent).toBe("Review complete, continue")
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Copy"])
  })

  it("writes the counts as they come, without plural rules", () => {
    const { container } = renderDone(1, 0)
    expect(container.querySelector(".done p")?.textContent).toBe("1 identities checked · 0 rejected")
  })

  it("copies the phrase for Codex and says Copied", async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal("navigator", { clipboard: { writeText } })
    const toast = vi.fn()
    renderDone(11, 4, fakeReview({ toast }))
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() => expect(toast.mock.calls).toEqual([["Copied"]]))
    expect(writeText.mock.calls).toEqual([["Review complete, continue"]])
  })

  it("names the phrase to type when the clipboard refuses", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: () => Promise.reject(new Error("denied")) } })
    const toastError = vi.fn()
    renderDone(11, 4, fakeReview({ toastError }))
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() =>
      expect(toastError.mock.calls).toEqual([["Copy failed — type: Review complete, continue"]]),
    )
  })
})
