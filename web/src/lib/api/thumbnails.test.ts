import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const photo = (id: number) => `https://media.licdn.com/dms/image/synthetic-${String(id)}`
const result = (...paths: (string | null)[]) => new Response(JSON.stringify({ paths }))

beforeEach(() => {
  vi.resetModules()
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("shared thumbnail client", () => {
  it("batches distinct photos and reuses queued, in-flight and completed requests", async () => {
    let respond: (response: Response) => void = () => {
      throw new Error("No request")
    }
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve
        }),
    )
    vi.stubGlobal("fetch", fetchMock)
    const { thumbnail } = await import("./thumbnails")
    const first = thumbnail(`${photo(1)}?expired=1`)
    const second = thumbnail(photo(2))
    expect(thumbnail(`${photo(1)}?new=2`)).toBe(first)
    expect(fetchMock).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(99)
    expect(fetchMock).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/profile-image/sign/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: [photo(1), photo(2)] }),
    })
    expect(thumbnail(photo(1))).toBe(first)
    respond(result("/profile-image?one", "/profile-image?two"))
    expect(await first).toBe("https://proxy.powerset.dev/profile-image?one")
    expect(await second).toBe("https://proxy.powerset.dev/profile-image?two")
    expect(thumbnail(photo(1))).toBe(first)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("sends at most 100 URLs per batch and drains arrivals during a request", async () => {
    let respond: (response: Response) => void = () => {
      throw new Error("No request")
    }
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve
        }),
    )
    vi.stubGlobal("fetch", fetchMock)
    const { thumbnail } = await import("./thumbnails")
    const requests = Array.from({ length: 100 }, (_, i) => thumbnail(photo(i)))
    await vi.advanceTimersByTimeAsync(100)
    requests.push(thumbnail(photo(100)))
    await vi.advanceTimersByTimeAsync(100)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    respond(result(...Array.from({ length: 100 }, () => "/profile-image?batch")))
    await vi.advanceTimersByTimeAsync(100)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    respond(result("/profile-image?last"))
    expect((await Promise.all(requests)).every(Boolean)).toBe(true)
  })

  it("preserves response order and caches null entries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(result(null, "/profile-image?two"))
    vi.stubGlobal("fetch", fetchMock)
    const { thumbnail } = await import("./thumbnails")
    const first = thumbnail(photo(1))
    const second = thumbnail(photo(2))
    await vi.advanceTimersByTimeAsync(100)
    expect(await first).toBeUndefined()
    expect(await second).toBe("https://proxy.powerset.dev/profile-image?two")
    expect(thumbnail(photo(1))).toBe(first)
  })

  it("settles failures, avoids scroll retry storms, and permits a later retry", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"))
    vi.stubGlobal("fetch", fetchMock)
    const { thumbnail } = await import("./thumbnails")
    const first = thumbnail(photo(1))
    await vi.advanceTimersByTimeAsync(100)
    expect(await first).toBeUndefined()
    expect(thumbnail(photo(1))).toBe(first)
    await vi.advanceTimersByTimeAsync(30_000)
    fetchMock.mockResolvedValue(result("/profile-image?retry"))
    const retry = thumbnail(photo(1))
    await vi.advanceTimersByTimeAsync(100)
    expect(await retry).toBe("https://proxy.powerset.dev/profile-image?retry")
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("refreshes cached signatures before their 30-day expiry", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(result("/profile-image?fresh")))
    vi.stubGlobal("fetch", fetchMock)
    const { thumbnail } = await import("./thumbnails")
    const first = thumbnail(photo(1))
    await vi.advanceTimersByTimeAsync(100)
    await first
    await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000)
    const next = thumbnail(photo(1))
    expect(next).not.toBe(first)
    await vi.advanceTimersByTimeAsync(100)
    await next
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
