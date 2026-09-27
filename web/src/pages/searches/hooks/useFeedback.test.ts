import { act, renderHook, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { buildSearchFeedback, FEEDBACK_STORAGE_KEY, formValues } from "@/lib/searches/feedback"
import { MemoryStorage } from "@/lib/searches/memory-storage"

import { useFeedback } from "./useFeedback"

const QUEUED = buildSearchFeedback("jordan-role", "Queued earlier")
const NEW = buildSearchFeedback("jordan-role", "Too senior")

function reply(status: string) {
  return Promise.resolve(new Response(JSON.stringify({ ok: true, status })))
}

function stored(): unknown {
  return JSON.parse(localStorage.getItem(FEEDBACK_STORAGE_KEY) ?? "null")
}

let fetchMock = vi.fn((_url: string, _init?: RequestInit) => reply("submitted"))

beforeEach(() => {
  fetchMock = vi.fn((_url: string, _init?: RequestInit) => reply("submitted"))
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("localStorage", new MemoryStorage())
})

afterEach(() => vi.unstubAllGlobals())

describe("useFeedback", () => {
  it("sends what was queued before on mount", async () => {
    localStorage.setItem(FEEDBACK_STORAGE_KEY, JSON.stringify([formValues(QUEUED)]))
    const { result } = renderHook(() => useFeedback())
    expect(result.current.pending).toHaveLength(1)
    await waitFor(() => expect(result.current.pending).toHaveLength(0))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(stored()).toEqual([])
  })

  it("says a submitted record was sent", async () => {
    const { result } = renderHook(() => useFeedback())
    let outcome = ""
    await act(async () => {
      outcome = await result.current.submit(NEW)
    })
    expect(outcome).toBe("sent")
    expect(result.current.pending).toEqual([])
  })

  it("keeps a record the network lost and sends it when the browser is back online", async () => {
    fetchMock.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")))
    const { result } = renderHook(() => useFeedback())
    let outcome = ""
    await act(async () => {
      outcome = await result.current.submit(NEW)
    })
    expect(outcome).toBe("queued")
    expect(result.current.pending).toEqual([NEW])
    expect(stored()).toEqual([formValues(NEW)])

    fetchMock.mockImplementation(() => reply("submitted"))
    act(() => {
      window.dispatchEvent(new Event("online"))
    })
    await waitFor(() => expect(result.current.pending).toEqual([]))
  })

  it("keeps a record the server saved but did not submit, until retry sends it", async () => {
    fetchMock.mockImplementation(() => reply("saved_locally"))
    const { result } = renderHook(() => useFeedback())
    await act(async () => {
      expect(await result.current.submit(NEW)).toBe("queued")
    })
    fetchMock.mockImplementation(() => reply("submitted"))
    await act(() => result.current.retry())
    expect(result.current.pending).toEqual([])
  })
})
