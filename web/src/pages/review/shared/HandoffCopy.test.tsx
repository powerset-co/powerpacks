import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { fakeReview } from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"

import { HandoffCopy } from "./HandoffCopy"
import { SynthesisPending } from "./SynthesisPending"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function stubClipboard(writeText: (text: string) => Promise<void>) {
  const write = vi.fn(writeText)
  vi.stubGlobal("navigator", { clipboard: { writeText: write } })
  return write
}

describe("HandoffCopy", () => {
  it("copies the phrase and says Copied", async () => {
    const write = stubClipboard(() => Promise.resolve())
    const toast = vi.fn()
    render(
      <ReviewHarness review={fakeReview({ toast })}>
        <HandoffCopy phrase="bin/deep-context-v2 run" />
      </ReviewHarness>,
    )
    expect(screen.getByText("bin/deep-context-v2 run").tagName).toBe("CODE")
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Copied"))
    expect(write).toHaveBeenCalledWith("bin/deep-context-v2 run")
  })

  it("names the phrase to type when the clipboard refuses", async () => {
    stubClipboard(() => Promise.reject(new Error("denied")))
    const toast = vi.fn()
    const toastError = vi.fn()
    render(
      <ReviewHarness review={fakeReview({ toast, toastError })}>
        <HandoffCopy phrase="Review complete, continue" />
      </ReviewHarness>,
    )
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Copy failed — type: Review complete, continue"),
    )
    expect(toast).not.toHaveBeenCalled()
  })

  it("fails the same way with no clipboard at all", async () => {
    vi.stubGlobal("navigator", {})
    const toastError = vi.fn()
    render(
      <ReviewHarness review={fakeReview({ toastError })}>
        <HandoffCopy phrase="bin/deep-context-v2 run" />
      </ReviewHarness>,
    )
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Copy failed — type: bin/deep-context-v2 run"),
    )
  })
})

describe("SynthesisPending", () => {
  it("shows the command to run instead of the screen", async () => {
    const write = stubClipboard(() => Promise.resolve())
    const { container } = render(
      <ReviewHarness review={fakeReview()}>
        <SynthesisPending />
      </ReviewHarness>,
    )
    expect(screen.getByRole("heading", { name: "Synthesis has not run" })).toBeTruthy()
    expect(container.querySelector(".empty-state p")?.textContent).toBe(
      "Collected messages have no facts yet. Go back to Codex and run:",
    )
    expect(container.querySelector(".empty-mark")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() => expect(write).toHaveBeenCalledWith("bin/deep-context-v2 run"))
  })
})
