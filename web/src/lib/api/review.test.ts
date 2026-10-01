import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  approveResult,
  changeEvent,
  decideResult,
  errorResponse,
  FakeEventSource,
  jsonResponse,
  reviewPage,
  reviewStatus,
  worthDetails,
  runningEvent,
  worthCard,
  worthPending,
  worthResult,
} from "@/testing/review-fixture"

import {
  approveEnrichment,
  completeStage,
  fetchDossier,
  fetchLinkedinCard,
  fetchReviewPage,
  fetchStatus,
  fetchWorthCard,
  fetchWorthPending,
  fetchWorthDetails,
  fetchWorthTable,
  openSignIn,
  postDecide,
  postFeedback,
  postRetarget,
  postWorth,
  ReviewError,
  watchEvents,
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
    answer(jsonResponse(reviewPage("worth")))
    expect((await fetchReviewPage("worth", "yes")).view).toBe("worth")
    expect(sent().url).toBe("/api/review/page?stage=worth&view=yes")
    expect(sent().init?.cache).toBe("no-store")
    fetchMock.mockClear()
    await fetchReviewPage("", "")
    expect(sent().url).toBe("/api/review/page")
  })

  it("reads the next worth card around the excluded keys", async () => {
    answer(jsonResponse(worthCard()))
    await fetchWorthCard({ exclude: ["worth-jordan", "worth-casey"] })
    expect(sent().url).toBe("/api/review/worth-card?exclude=worth-jordan%2Cworth-casey")
  })

  it("reads a picked card, and a carousel position", async () => {
    answer(jsonResponse(worthCard()))
    await fetchWorthCard({ pick: "worth-casey" })
    expect(sent().url).toBe("/api/review/worth-card?pick=worth-casey")
    fetchMock.mockClear()
    await fetchWorthCard({ index: 2, debug: true })
    expect(sent().url).toBe("/api/review/worth-card?index=2&debug=1")
    fetchMock.mockClear()
    await fetchWorthCard()
    expect(sent().url).toBe("/api/review/worth-card")
  })

  it("tells a picked card that is no longer pending from any other failure", async () => {
    answer(errorResponse("gone", 404))
    const gone = await refused(fetchWorthCard({ pick: "worth-casey" }))
    expect(gone.gone).toBe(true)
    expect(gone.message).toBe("gone")
    answer(new Response("boom", { status: 500 }))
    expect((await refused(fetchWorthCard({ pick: "worth-casey" }))).gone).toBe(false)
  })

  it("reads the typeahead's names, a pile's page and the LinkedIn card", async () => {
    answer(jsonResponse({ pending: worthPending() }))
    expect(await fetchWorthPending()).toHaveLength(3)
    expect(sent().url).toBe("/api/review/worth-pending")
    fetchMock.mockClear()
    answer(jsonResponse({ rows: [], total: 0 }))
    await fetchWorthTable("no", 100)
    expect(sent().url).toBe("/api/review/worth-table?view=no&offset=100")
    fetchMock.mockClear()
    await fetchWorthTable("yes", 0)
    expect(sent().url).toBe("/api/review/worth-table?view=yes&offset=0")
    fetchMock.mockClear()
    await fetchLinkedinCard({ exclude: ["jordan-bravo"] })
    expect(sent().url).toBe("/api/review/linkedin-card?exclude=jordan-bravo")
  })

  it("reads an opened row's details by slug, and says when the parent is gone", async () => {
    answer(jsonResponse(worthDetails()))
    expect((await fetchWorthDetails("jordan bravo")).person.slug).toBe("jordan-bravo")
    expect(sent().url).toBe("/api/review/worth-details?slug=jordan+bravo")
    answer(errorResponse("gone", 404))
    expect((await refused(fetchWorthDetails("jordan-bravo"))).gone).toBe(true)
  })

  it("reads the status", async () => {
    answer(jsonResponse(reviewStatus({ stage: "linkedin" })))
    expect((await fetchStatus()).stage).toBe("linkedin")
    expect(sent().url).toBe("/api/status")
    expect(sent().init?.cache).toBe("no-store")
  })

  it("carries the server's words for a refused read", async () => {
    answer(errorResponse("view must be yes or no", 400))
    const error = await refused(fetchWorthTable("yes", 0))
    expect(error.message).toBe("view must be yes or no")
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
  it("posts a worth decision as form fields, the note riding along", async () => {
    answer(jsonResponse(worthResult()))
    const result = await postWorth({
      pub: "worth-jordan",
      worth: "yes",
      parent_slug: "jordan-bravo",
      note: "met at Acme",
    })
    expect(result.progress.worth_pending).toBe(2)
    expect(sent().url).toBe("/worth")
    expect(sent().init?.method).toBe("POST")
    expect(sent().init?.headers).toEqual({ "Content-Type": "application/x-www-form-urlencoded" })
    expect(sent().form).toBe("pub=worth-jordan&worth=yes&parent_slug=jordan-bravo&note=met+at+Acme")
  })

  it("leaves the note out of a table flip", async () => {
    answer(jsonResponse(worthResult()))
    await postWorth({ pub: "worth-jordan", worth: "no", parent_slug: "jordan-bravo" })
    expect(sent().form).toBe("pub=worth-jordan&worth=no&parent_slug=jordan-bravo")
  })

  it("posts a LinkedIn decision to the JSON route; a fix carries the URL", async () => {
    answer(jsonResponse(decideResult()))
    expect(
      (await postDecide({ pub: "jordan-bravo-1", decision: "keep", parent_slug: "jordan-bravo" })).next
        .pending,
    ).toBe(3)
    expect(sent().url).toBe("/api/review/decide")
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

  it("approves the enrichment with one empty post", async () => {
    answer(jsonResponse(approveResult()))
    expect((await approveEnrichment()).enrichment.mode).toBe("running")
    expect(sent().url).toBe("/api/review/approve-enrichment")
    expect(sent().form).toBe("")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("completes a stage, queues a re-research and opens the sign-in", async () => {
    answer(jsonResponse({ ok: true }))
    await completeStage("enrich")
    expect([sent().url, sent().form]).toEqual(["/complete", "stage=enrich"])
    fetchMock.mockClear()
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
    answer(new Response("stale or mismatched person card", { status: 409 }))
    const stale = await refused(postWorth({ pub: "worth-jordan", worth: "yes", parent_slug: "jordan-bravo" }))
    expect([stale.message, stale.http, stale.status]).toEqual(["stale or mismatched person card", 409, ""])
    answer(new Response("", { status: 500 }))
    expect((await refused(completeStage("worth"))).message).toBe("Could not save")
  })
})

describe("watchEvents", () => {
  beforeEach(() => {
    FakeEventSource.opened = []
    vi.stubGlobal("EventSource", FakeEventSource)
  })

  it("hears each message and each connect until closed", () => {
    const onEvent = vi.fn()
    const onOpen = vi.fn()
    const close = watchEvents(onEvent, onOpen)
    const [stream] = FakeEventSource.opened
    expect(stream?.url).toBe("/api/events")
    stream?.open()
    stream?.emit(runningEvent(3, 12))
    stream?.emit(changeEvent())
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onEvent.mock.calls).toEqual([[runningEvent(3, 12)], [changeEvent()]])
    close()
    expect(stream?.closed).toBe(true)
  })

  it("passes an unreadable message as null", () => {
    const onEvent = vi.fn()
    watchEvents(onEvent, vi.fn())
    FakeEventSource.opened[0]?.emit("not json")
    FakeEventSource.opened[0]?.emit("[1]")
    expect(onEvent.mock.calls).toEqual([[null], [null]])
  })
})
