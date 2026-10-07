import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { decideResult, errorResponse, jsonResponse, linkedinCard, reviewPage } from "@/testing/review-fixture"

import {
  fetchDossier,
  fetchLinkedinCard,
  fetchReviewPage,
  openSignIn,
  postDecide,
  postFeedback,
  postRetarget,
  ReviewError,
} from "./review"

const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>()

function answer(response: Response) {
  fetchMock.mockImplementation(() => Promise.resolve(response.clone()))
}

/** The one request the test made. */
function sent() {
  const [url, init] = fetchMock.mock.calls[0] ?? []
  return { url, init, form: init?.body instanceof URLSearchParams ? init.body.toString() : null }
}

async function refused(request: Promise<unknown>): Promise<ReviewError> {
  const error: unknown = await request.catch((caught: unknown) => caught)
  if (!(error instanceof ReviewError)) throw new Error("the request was not refused")
  return error
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal("fetch", fetchMock)
})
afterEach(() => vi.unstubAllGlobals())

describe("the review reads", () => {
  it("asks for the page with the URL's stage and view, and for the store's stage with neither", async () => {
    answer(jsonResponse(reviewPage()))
    expect((await fetchReviewPage("linkedin", "")).view).toBe("linkedin")
    expect(sent().url).toBe("/api/review/page?stage=linkedin")
    expect(sent().init?.cache).toBe("no-store")
    fetchMock.mockClear()
    await fetchReviewPage("", "")
    expect(sent().url).toBe("/api/review/page")
  })

  it("reads the next LinkedIn card around the excluded slugs, and a carousel position", async () => {
    answer(jsonResponse(linkedinCard()))
    await fetchLinkedinCard({ exclude: ["jordan-bravo", "casey-delta"] })
    expect(sent().url).toBe("/api/review/linkedin-card?exclude=jordan-bravo%2Ccasey-delta")
    fetchMock.mockClear()
    await fetchLinkedinCard({ index: 2, debug: true })
    expect(sent().url).toBe("/api/review/linkedin-card?index=2&debug=1")
    fetchMock.mockClear()
    await fetchLinkedinCard()
    expect(sent().url).toBe("/api/review/linkedin-card")
  })

  it("tells a person who is no longer pending from any other failure", async () => {
    answer(errorResponse("gone", 404))
    const gone = await refused(fetchLinkedinCard())
    expect(gone.gone).toBe(true)
    expect(gone.message).toBe("gone")
    answer(new Response("boom", { status: 500 }))
    expect((await refused(fetchLinkedinCard())).gone).toBe(false)
  })

  it("carries the server's words for a refused read", async () => {
    answer(errorResponse("index must be a number", 400))
    const error = await refused(fetchLinkedinCard({ index: 2 }))
    expect(error.message).toBe("index must be a number")
    expect(error.http).toBe(400)
  })
})

describe("the dossier", () => {
  it("is the server's HTML, asked for without the name and contact", async () => {
    answer(new Response("<h4>Summary</h4>"))
    expect(await fetchDossier("jordan-bravo")).toBe("<h4>Summary</h4>")
    expect(sent().url).toBe("/api/dossier?slug=jordan-bravo&skip=1")
  })

  it("is null when the server has none, and throws when the request fails", async () => {
    answer(new Response("", { status: 404 }))
    expect(await fetchDossier("jordan-bravo")).toBeNull()
    fetchMock.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")))
    await expect(fetchDossier("jordan-bravo")).rejects.toThrow("Failed to fetch")
  })
})

describe("the review writes", () => {
  it("posts a LinkedIn decision as form fields to the JSON route; a fix carries the URL", async () => {
    answer(jsonResponse(decideResult()))
    expect(
      (await postDecide({ pub: "jordan-bravo-1", decision: "keep", parent_slug: "jordan-bravo" })).next
        .pending,
    ).toBe(3)
    expect(sent().url).toBe("/api/review/decide")
    expect(sent().init?.method).toBe("POST")
    expect(sent().init?.headers).toEqual({ "Content-Type": "application/x-www-form-urlencoded" })
    expect(sent().form).toBe("pub=jordan-bravo-1&decision=keep&parent_slug=jordan-bravo")
    fetchMock.mockClear()
    await postDecide({
      pub: "jordan-bravo-1",
      decision: "fix",
      parent_slug: "jordan-bravo",
      new_url: "linkedin.com/in/jordan-bravo-2",
    })
    expect(sent().form).toBe(
      "pub=jordan-bravo-1&decision=fix&parent_slug=jordan-bravo&new_url=linkedin.com%2Fin%2Fjordan-bravo-2",
    )
  })

  it("queues a re-research and opens the sign-in", async () => {
    answer(jsonResponse({ ok: true }))
    await postRetarget({ pub: "jordan-bravo-1", parent_slug: "jordan-bravo", guidance: "the founder" })
    expect([sent().url, sent().form]).toEqual([
      "/retarget",
      "pub=jordan-bravo-1&parent_slug=jordan-bravo&guidance=the+founder",
    ])
    fetchMock.mockClear()
    await openSignIn()
    expect([sent().url, sent().form]).toEqual(["/auth/login", ""])
  })

  it("files feedback", async () => {
    answer(jsonResponse({ ok: true, status: "submitted" }))
    await postFeedback({
      pub: "jordan-bravo-1",
      parent_slug: "jordan-bravo",
      comment: "wrong person",
      action: "general",
    })
    expect([sent().url, sent().form]).toEqual([
      "/feedback",
      "pub=jordan-bravo-1&parent_slug=jordan-bravo&comment=wrong+person&action=general",
    ])
  })

  it("keeps a feedback failure that needs a sign-in apart from any other", async () => {
    const feedback = {
      pub: "jordan-bravo-1",
      parent_slug: "jordan-bravo",
      comment: "x",
      action: "general",
    } as const
    answer(jsonResponse({ ok: false, status: "needs_auth", error: "not signed in to Powerset" }, 502))
    const unsigned = await refused(postFeedback(feedback))
    expect(unsigned.needsAuth).toBe(true)
    expect(unsigned.message).toBe("not signed in to Powerset")
    answer(jsonResponse({ ok: false, status: "failed", error: "Powerset is down" }, 502))
    expect((await refused(postFeedback(feedback))).needsAuth).toBe(false)
  })

  it("says the status when a JSON failure has no message", async () => {
    answer(jsonResponse({ ok: false, status: "needs_auth", error: "" }, 502))
    expect((await refused(openSignIn())).message).toBe("needs_auth")
  })

  it("shows a text failure as written, and its own words for an empty one", async () => {
    const decision = { pub: "jordan-bravo-1", decision: "keep", parent_slug: "jordan-bravo" } as const
    answer(new Response("stale or mismatched person card", { status: 409 }))
    const stale = await refused(postDecide(decision))
    expect([stale.message, stale.http, stale.status]).toEqual(["stale or mismatched person card", 409, ""])
    answer(new Response("", { status: 500 }))
    const empty = await refused(postDecide(decision))
    expect(empty.message).toBe("Could not save")
  })
})
