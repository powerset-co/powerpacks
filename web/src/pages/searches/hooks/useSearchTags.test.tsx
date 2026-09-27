import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Tagged } from "@/types/searches"

import { useSearchTags } from "./useSearchTags"

const RUN_ID = "jordan-role"

function respond(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status })
}

/** The Tagged a POST /searches/tags carried. */
function sent(init?: RequestInit): unknown {
  if (!(init?.body instanceof URLSearchParams)) throw new Error("the tags request carried no form")
  return JSON.parse(init.body.get("tagged") ?? "null")
}

interface Server {
  fetch: ReturnType<typeof vi.fn>
  posts: RequestInit[]
  // Answers the oldest unanswered save.
  answer: (response: Response) => void
}

/** GET answers `initial` at once; each POST waits until the test answers it. */
function server(initial: Tagged | null): Server {
  const posts: RequestInit[] = []
  const waiting: ((response: Response) => void)[] = []
  const fetch = vi.fn((_url: string, init?: RequestInit) => {
    if (init?.method !== "POST") return Promise.resolve(respond({ tagged: initial }))
    posts.push(init)
    return new Promise<Response>((resolve) => waiting.push(resolve))
  })
  vi.stubGlobal("fetch", fetch)
  return { fetch, posts, answer: (response) => waiting.shift()?.(response) }
}

function renderTags() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return renderHook(() => useSearchTags(RUN_ID), { wrapper })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("useSearchTags", () => {
  it("loads the run's tags, and none when the server has none", async () => {
    server(null)
    const { result } = renderTags()
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.tagged).toEqual({ tags: [], assignments: {} })
  })

  it("shows an edit at once and saves one request at a time, each carrying every edit so far", async () => {
    const api = server({ tags: [], assignments: {} })
    const { result } = renderTags()
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.toggle("p-jordan", "First"))
    act(() => result.current.toggle("p-jordan", "Second"))
    await waitFor(() => expect(result.current.tagged.assignments["p-jordan"]).toEqual(["First", "Second"]))
    await waitFor(() => expect(api.posts).toHaveLength(1))
    expect(sent(api.posts[0])).toEqual({ tags: ["First"], assignments: { "p-jordan": ["First"] } })

    await act(async () => {
      api.answer(respond({ ok: true }))
      await Promise.resolve()
    })
    await waitFor(() => expect(api.posts).toHaveLength(2))
    expect(sent(api.posts[1])).toEqual({
      tags: ["First", "Second"],
      assignments: { "p-jordan": ["First", "Second"] },
    })
    api.answer(respond({ ok: true }))
  })

  it("puts back the saved tags and says so when a save fails", async () => {
    const api = server({ tags: ["Backend"], assignments: { "p-casey": ["Backend"] } })
    const { result } = renderTags()
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.clear())
    await waitFor(() => expect(result.current.tagged).toEqual({ tags: [], assignments: {} }))
    await waitFor(() => expect(api.posts).toHaveLength(1))
    await act(async () => {
      api.answer(respond({ error: "disk full" }, 500))
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.toast?.message).toBe("Tags not saved: disk full"))
    await waitFor(() =>
      expect(result.current.tagged).toEqual({ tags: ["Backend"], assignments: { "p-casey": ["Backend"] } }),
    )
  })

  it("keeps a newer edit when an older save fails", async () => {
    const api = server({ tags: [], assignments: {} })
    const { result } = renderTags()
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.toggle("p-jordan", "First"))
    act(() => result.current.toggle("p-casey", "Second"))
    await waitFor(() => expect(api.posts).toHaveLength(1))
    await act(async () => {
      api.answer(respond({ error: "busy" }, 500))
      await Promise.resolve()
    })
    await waitFor(() => expect(api.posts).toHaveLength(2))
    expect(result.current.tagged.assignments).toEqual({ "p-jordan": ["First"], "p-casey": ["Second"] })
    api.answer(respond({ ok: true }))
  })

  it("removes a tag, untags people, and clears everything", async () => {
    const api = server({ tags: ["A", "B"], assignments: { a: ["A", "B"], b: ["B"] } })
    const { result } = renderTags()
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.remove("B"))
    await waitFor(() => expect(result.current.tagged).toEqual({ tags: ["A"], assignments: { a: ["A"] } }))
    act(() => result.current.untag(["a"]))
    await waitFor(() => expect(result.current.tagged).toEqual({ tags: ["A"], assignments: {} }))
    act(() => result.current.clear())
    await waitFor(() => expect(result.current.tagged).toEqual({ tags: [], assignments: {} }))
    await waitFor(() => expect(api.posts).toHaveLength(1))
    api.answer(respond({ ok: true }))
  })
})
