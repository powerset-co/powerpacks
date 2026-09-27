import { afterEach, describe, expect, it, vi } from "vitest"

import { buildSearchFeedback } from "@/lib/searches/feedback"

import { postFeedback } from "./feedback"

afterEach(() => vi.unstubAllGlobals())

describe("postFeedback", () => {
  it("posts the record as form fields and returns the reply", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(new Response(JSON.stringify({ ok: true, status: "submitted" }))),
    )
    vi.stubGlobal("fetch", fetchMock)
    expect(await postFeedback(buildSearchFeedback("jordan-role", "Too senior"))).toEqual({
      ok: true,
      status: "submitted",
    })
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe("/searches/feedback")
    expect(init?.body).toBeInstanceOf(URLSearchParams)
    expect(init?.body instanceof URLSearchParams ? init.body.toString() : "").toBe(
      "run_id=jordan-role&person_id=&comment=Too+senior",
    )
  })

  it("carries the server's error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("candidate not found", { status: 404 }))),
    )
    await expect(postFeedback(buildSearchFeedback("jordan-role", "x"))).rejects.toThrow("candidate not found")
  })
})
