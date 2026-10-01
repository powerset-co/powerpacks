import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useReviewToast } from "./useReviewToast"

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe("useReviewToast", () => {
  it("shows a message for 1.8 s", () => {
    const { result } = renderHook(() => useReviewToast())
    act(() => result.current.say("Added"))
    expect(result.current.toast).toEqual({ message: "Added" })
    act(() => void vi.advanceTimersByTime(1799))
    expect(result.current.toast).not.toBeNull()
    act(() => void vi.advanceTimersByTime(1))
    expect(result.current.toast).toBeNull()
  })

  it("shows an error for 6 s", () => {
    const { result } = renderHook(() => useReviewToast())
    act(() => result.current.sayError("stale or mismatched person card"))
    expect(result.current.toast).toEqual({ message: "stale or mismatched person card", error: true })
    act(() => void vi.advanceTimersByTime(5999))
    expect(result.current.toast).not.toBeNull()
    act(() => void vi.advanceTimersByTime(1))
    expect(result.current.toast).toBeNull()
  })

  it("replaces the old message and starts its clock over, even for the same words", () => {
    const { result } = renderHook(() => useReviewToast())
    act(() => result.current.say("Added"))
    act(() => void vi.advanceTimersByTime(1000))
    act(() => result.current.say("Added"))
    act(() => void vi.advanceTimersByTime(1000))
    expect(result.current.toast).toEqual({ message: "Added" })
    act(() => result.current.sayError("Could not save"))
    act(() => void vi.advanceTimersByTime(1800))
    expect(result.current.toast).toEqual({ message: "Could not save", error: true })
  })
})
