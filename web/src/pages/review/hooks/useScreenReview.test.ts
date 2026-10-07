import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { decisionProgress, motionMedia, pageProgress, reviewPage } from "@/testing/review-fixture"

import type { Screen } from "./useScreen"
import { useScreenReview } from "./useScreenReview"

// A worth screen: nothing here watches the server, so no request leaves the hook.
const SCREEN: Screen = {
  id: 1,
  page: reviewPage("worth", { progress: pageProgress({ worth_pending: 3, worth_yes: 5 }) }),
  preview: true,
  debug: true,
  index: 2,
}

function renderReview(reducedMotion = false) {
  vi.stubGlobal("matchMedia", motionMedia(reducedMotion))
  const toast = { toast: null, dismiss: vi.fn(), say: vi.fn(), sayError: vi.fn() }
  const reload = vi.fn()
  const open = vi.fn()
  const hook = renderHook(() => useScreenReview({ screen: SCREEN, toast, reload, open }))
  return { ...hook, toast, reload, open }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("useScreenReview", () => {
  it("hands the stage the screen's settings and the page's toast", () => {
    const { result, toast } = renderReview()
    const { review } = result.current
    expect([review.preview, review.debug, review.index, review.fadeMs]).toEqual([true, true, 2, 150])
    review.toast("Added")
    review.toastError("Could not save")
    expect(toast.say).toHaveBeenCalledWith("Added")
    expect(toast.sayError).toHaveBeenCalledWith("Could not save")
  })

  it("has no fade wait under reduced motion", () => {
    expect(renderReview(true).result.current.review.fadeMs).toBe(0)
  })

  it("repaints the counts from a click response and keeps what only the page load knows", () => {
    const { result } = renderReview()
    act(() => result.current.review.applyProgress(decisionProgress({ worth_pending: 2, worth_yes: 6 })))
    expect(result.current.progress).toMatchObject({
      worth_pending: 2,
      worth_yes: 6,
      linkedin_done: 6,
      rejected: 2,
    })
    expect(result.current.review.progress).toBe(result.current.progress)
  })

  it("holds the stage check for 650 ms, then opens the next stage's screen", () => {
    const { result, open } = renderReview()
    expect(result.current.check).toBeNull()
    act(() => result.current.review.transition("People Reviewed", "enrich"))
    expect(result.current.check).toBe("People Reviewed")
    act(() => void vi.advanceTimersByTime(649))
    expect(open).not.toHaveBeenCalled()
    act(() => void vi.advanceTimersByTime(1))
    expect(open).toHaveBeenCalledWith("/?stage=enrich")
    // The check stays until the next screen loads (this one unmounts then).
    expect(result.current.check).toBe("People Reviewed")
  })

  it("holds a wordless check as long, reduced motion or not", () => {
    const { result, open } = renderReview(true)
    act(() => result.current.review.transition("", "linkedin"))
    expect(result.current.check).toBe("")
    act(() => void vi.advanceTimersByTime(649))
    expect(open).not.toHaveBeenCalled()
    act(() => void vi.advanceTimersByTime(1))
    expect(open).toHaveBeenCalledWith("/?stage=linkedin")
  })

  it("says the message, fades the stage out for 150 ms, then reloads", () => {
    const { result, toast, reload } = renderReview()
    act(() => result.current.review.leaveAndReload("Saved"))
    expect(toast.say).toHaveBeenCalledWith("Saved")
    expect(result.current.leaving).toBe(true)
    act(() => void vi.advanceTimersByTime(149))
    expect(reload).not.toHaveBeenCalled()
    act(() => void vi.advanceTimersByTime(1))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("reloads at once under reduced motion", () => {
    const { result, reload } = renderReview(true)
    act(() => result.current.review.leaveAndReload("Saved"))
    act(() => void vi.advanceTimersByTime(0))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("drops a pending transition when the screen goes away", () => {
    const { result, open, unmount } = renderReview()
    act(() => result.current.review.transition("People Reviewed", "enrich"))
    unmount()
    vi.advanceTimersByTime(650)
    expect(open).not.toHaveBeenCalled()
  })
})
