import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { useEffect } from "react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import {
  changeEvent,
  decisionProgress,
  enrichmentPanel,
  errorResponse,
  FakeEventSource,
  jsonResponse,
  motionMedia,
  pageProgress,
  reviewPage,
  reviewStatus,
  runningEvent,
} from "@/testing/review-fixture"
import type { ReviewPage as ReviewPageData, ReviewStatus } from "@/types/review"

import type { DoneStageProps } from "./done/DoneStage"
import type { EnrichStageProps } from "./enrich/EnrichStage"
import { useReview, type Review } from "./hooks/useReview"
import { ReviewPage } from "./ReviewPage"
import type { WorthStageProps } from "./worth/WorthStage"

// The stages are other suites' business: each is a probe here that shows the props the page
// gave it and hands the test the page's `Review`.
const seen = vi.hoisted((): { review: Review | null; mounts: number } => ({ review: null, mounts: 0 }))

function Probe({ stage, props }: { stage: string; props: object }) {
  const review = useReview()
  useEffect(() => {
    seen.review = review
  })
  useEffect(() => {
    seen.mounts += 1
  }, [])
  return <div data-probe={stage}>{JSON.stringify(props)}</div>
}

vi.mock("./worth/WorthStage", () => ({
  WorthStage: (props: WorthStageProps) => <Probe stage="worth" props={props} />,
}))
vi.mock("./enrich/EnrichStage", () => ({
  EnrichStage: (props: EnrichStageProps) => <Probe stage="enrich" props={props} />,
}))
vi.mock("./linkedin/LinkedinStage", () => ({
  LinkedinStage: () => <Probe stage="linkedin" props={{}} />,
}))
vi.mock("./done/DoneStage", () => ({
  DoneStage: (props: DoneStageProps) => <Probe stage="done" props={props} />,
}))

/** The review server: one page per `stage` asked for ("" is the store's own stage), and a status. */
const server: { pages: Record<string, ReviewPageData | Response>; status: ReviewStatus } = {
  pages: {},
  status: reviewStatus(),
}

const fetchMock = vi.fn((url: string, _init?: RequestInit): Promise<Response> => {
  const { pathname, searchParams } = new URL(url, "http://review.test")
  if (pathname === "/api/status") return Promise.resolve(jsonResponse(server.status))
  if (pathname !== "/api/review/page") return Promise.reject(new Error(`unexpected request: ${url}`))
  const page = must(server.pages[searchParams.get("stage") ?? ""], `a page for ${url}`)
  return Promise.resolve(page instanceof Response ? page.clone() : jsonResponse(page))
})

const requests = (path: string) =>
  fetchMock.mock.calls.map(([url]) => url).filter((url) => url.startsWith(path))
const review = () => must(seen.review, "the stage's review")
const probe = (stage: string) => document.querySelector(`[data-probe='${stage}']`)
const probeProps = (stage: string): unknown =>
  JSON.parse(must(probe(stage), `the ${stage} stage`).textContent)
const stream = () => must(FakeEventSource.opened[0], "the event stream")
const stepText = () => screen.getAllByRole("link").map((link) => link.textContent)

function Where() {
  const location = useLocation()
  return <span data-where>{location.pathname + location.search}</span>
}
const where = () => document.querySelector("[data-where]")?.textContent

function renderPage(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/"
            element={
              <>
                <ReviewPage />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  seen.review = null
  seen.mounts = 0
  server.pages = {
    "": reviewPage("worth"),
    worth: reviewPage("worth"),
    enrich: reviewPage("enrich"),
    linkedin: reviewPage("linkedin"),
    done: reviewPage("done"),
  }
  server.status = reviewStatus()
  FakeEventSource.opened = []
  fetchMock.mockClear()
  vi.stubGlobal("fetch", fetchMock)
  vi.stubGlobal("EventSource", FakeEventSource)
  vi.stubGlobal("matchMedia", motionMedia())
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ReviewPage: each screen", () => {
  it("opens the worth screen on the tab the URL names", async () => {
    server.pages.worth = reviewPage("worth", { tab: "yes" })
    const { container } = renderPage("/?stage=worth&view=yes&preview=1&debug=1&index=3")
    await waitFor(() => expect(probeProps("worth")).toEqual({ tab: "yes" }))
    expect(requests("/api/review/page")).toEqual(["/api/review/page?stage=worth&view=yes"])
    const root = must(container.querySelector(".review-page"))
    expect(root.getAttribute("data-stage")).toBe("worth")
    expect(root.getAttribute("data-preview")).toBe("true")
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Add People")
    expect(document.title).toBe("Add People · Powerpacks")
    expect([review().preview, review().debug, review().index]).toEqual([true, true, 3])
  })

  it("lands on the store's stage when the URL names none", async () => {
    server.pages[""] = reviewPage("linkedin")
    const { container } = renderPage("/")
    await waitFor(() => expect(probe("linkedin")).toBeTruthy())
    expect(requests("/api/review/page")).toEqual(["/api/review/page"])
    expect(container.querySelector(".review-page")?.getAttribute("data-stage")).toBe("linkedin")
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Check LinkedIn")
    expect(document.title).toBe("Check LinkedIn · Powerpacks")
    expect([review().preview, review().debug, review().index]).toEqual([false, false, 0])
  })

  it("shows Enrich when the worth queue was asked for but the server says it is empty", async () => {
    server.pages.worth = reviewPage("enrich")
    renderPage("/?stage=worth")
    await waitFor(() => expect(probe("enrich")).toBeTruthy())
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Enrich Contacts")
  })

  it("gives the Enrich stage its panel", async () => {
    const enrichment = enrichmentPanel({ mode: "running", completed: 2, total: 9 })
    server.pages.enrich = reviewPage("enrich", { enrichment })
    renderPage("/?stage=enrich")
    await waitFor(() => expect(probeProps("enrich")).toEqual({ enrichment, job: null }))
    expect(document.title).toBe("Enrich Contacts · Powerpacks")
  })

  it("gives the Done stage its two counts", async () => {
    server.pages.done = reviewPage("done", { progress: pageProgress({ linkedin_done: 11, rejected: 4 }) })
    renderPage("/?stage=done")
    await waitFor(() => expect(probeProps("done")).toEqual({ checked: 11, rejected: 4 }))
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("All Set")
  })

  it("shows the synthesis handoff instead of Done, and of Enrich once its plan is complete", async () => {
    const progress = pageProgress({ synthesize_pending: 4 })
    server.pages.done = reviewPage("done", { progress, needs_synthesis: true })
    server.pages.enrich = reviewPage("enrich", { progress, needs_synthesis: true })
    const first = renderPage("/?stage=done")
    await waitFor(() => expect(screen.getByRole("heading", { name: "Synthesis has not run" })).toBeTruthy())
    expect(probe("done")).toBeNull()
    expect(screen.getByText("bin/deep-context dry")).toBeTruthy()
    first.unmount()
    renderPage("/?stage=enrich")
    await waitFor(() => expect(screen.getByRole("heading", { name: "Synthesis has not run" })).toBeTruthy())
    expect(probe("enrich")).toBeNull()
  })

  it("keeps the Enrich panel while synthesis is pending and the plan is not complete", async () => {
    server.pages.enrich = reviewPage("enrich", { progress: pageProgress({ synthesize_pending: 4 }) })
    renderPage("/?stage=enrich")
    await waitFor(() => expect(probe("enrich")).toBeTruthy())
  })

  it("draws its own top bar: the brand opens the worth stage, and no page tabs", async () => {
    const { container } = renderPage("/?stage=linkedin")
    await waitFor(() => expect(probe("linkedin")).toBeTruthy())
    const brand = must(container.querySelector(".topbar .brand"))
    expect(brand.textContent).toBe("POWERPACKS")
    expect(brand.getAttribute("href")).toBe("/?stage=worth")
    expect(container.querySelector("[data-nav]")).toBeNull()
    expect(screen.getAllByRole("navigation")).toHaveLength(1)
  })

  it("says so when the screen cannot load", async () => {
    server.pages.worth = errorResponse("review store is locked", 500)
    renderPage("/?stage=worth")
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Could not load the review" })).toBeTruthy(),
    )
    expect(screen.getByText("review store is locked")).toBeTruthy()
  })
})

describe("ReviewPage: the stepper", () => {
  it("shows the three steps with their counts and lights the screen's step", async () => {
    server.pages.enrich = reviewPage("enrich", {
      progress: pageProgress({ worth_pending: 0, linkedin_pending: 4 }),
    })
    renderPage("/?stage=enrich")
    await waitFor(() => expect(probe("enrich")).toBeTruthy())
    expect(stepText()).toEqual([
      "POWERPACKS",
      "✓Review Decisions",
      "2Enrich Contacts",
      "3Check LinkedIn4 left",
    ])
    expect(screen.getByRole("link", { name: /Enrich Contacts/ }).className).toBe("step active")
  })

  it("repaints the step badges from a click response without reading the page again", async () => {
    renderPage("/?stage=worth")
    await waitFor(() => expect(probe("worth")).toBeTruthy())
    expect(stepText().slice(1)).toEqual([
      "1Review Decisions3 left",
      "2Enrich Contacts",
      "3Check LinkedIn4 left",
    ])
    act(() =>
      review().applyProgress(decisionProgress({ worth_pending: 2, worth_yes: 6, linkedin_pending: 0 })),
    )
    expect(stepText().slice(1)).toEqual(["1Review Decisions2 left", "2Enrich Contacts", "3Check LinkedIn"])
    expect(review().progress).toMatchObject({ worth_pending: 2, worth_yes: 6, worth_no: 2, linkedin_done: 6 })
    expect(requests("/api/review/page")).toHaveLength(1)
    expect(seen.mounts).toBe(1)
  })

  it("opens a step's stage in place: the URL changes and the page is read again", async () => {
    renderPage("/?stage=worth")
    await waitFor(() => expect(probe("worth")).toBeTruthy())
    fireEvent.click(screen.getByRole("link", { name: /Check LinkedIn/ }))
    await waitFor(() => expect(probe("linkedin")).toBeTruthy())
    expect(probe("worth")).toBeNull()
    expect(where()).toBe("/?stage=linkedin&preview=1")
    expect(requests("/api/review/page")).toEqual([
      "/api/review/page?stage=worth",
      "/api/review/page?stage=linkedin",
    ])
    expect(review().preview).toBe(true)
    expect(document.title).toBe("Check LinkedIn · Powerpacks")
  })

  it("reads the page again for a link to the screen already open", async () => {
    renderPage("/?stage=worth&preview=1")
    await waitFor(() => expect(probe("worth")).toBeTruthy())
    fireEvent.click(screen.getByRole("link", { name: /Review Decisions/ }))
    await waitFor(() => expect(requests("/api/review/page")).toHaveLength(2))
    await waitFor(() => expect(seen.mounts).toBe(2))
  })
})

describe("ReviewPage: the toast", () => {
  it("shows a stage's message and replaces it with the next", async () => {
    renderPage("/?stage=worth")
    await waitFor(() => expect(probe("worth")).toBeTruthy())
    act(() => review().toast("Added"))
    expect(screen.getByRole("status").textContent).toBe("Added")
    act(() => review().toastError("stale or mismatched person card"))
    expect(screen.getByRole("status").textContent).toBe("stale or mismatched person card")
  })
})

describe("ReviewPage: the stage transition", () => {
  it("shows the check, then the next stage's screen", async () => {
    renderPage("/?stage=worth&preview=1")
    await waitFor(() => expect(probe("worth")).toBeTruthy())
    act(() => review().transition("People Reviewed", "enrich"))
    expect(probe("worth")).toBeNull()
    expect(screen.getByRole("heading", { name: "People Reviewed" }).closest(".stage-complete")).toBeTruthy()
    expect(requests("/api/review/page")).toHaveLength(1)
    await waitFor(() => expect(probe("enrich")).toBeTruthy(), { timeout: 2000 })
    expect(where()).toBe("/?stage=enrich")
    expect(screen.queryByRole("heading", { name: "People Reviewed" })).toBeNull()
    expect(review().preview).toBe(false)
  })

  it("reads the same stage again when LinkedIn finishes", async () => {
    renderPage("/?stage=linkedin")
    await waitFor(() => expect(probe("linkedin")).toBeTruthy())
    act(() => review().transition("", "linkedin"))
    expect(probe("linkedin")).toBeNull()
    expect(document.querySelector(".stage-complete .empty-mark")?.textContent).toBe("✓")
    await waitFor(() => expect(requests("/api/review/page")).toHaveLength(2), { timeout: 2000 })
    await waitFor(() => expect(probe("linkedin")).toBeTruthy())
    expect(document.querySelector(".stage-complete")).toBeNull()
  })

  it("fades the stage and reads the screen again on leave-and-reload", async () => {
    renderPage("/?stage=worth")
    await waitFor(() => expect(probe("worth")).toBeTruthy())
    act(() => review().leaveAndReload("Saved"))
    expect(screen.getByRole("status").textContent).toBe("Saved")
    await waitFor(() => expect(requests("/api/review/page")).toHaveLength(2))
    await waitFor(() => expect(seen.mounts).toBe(2))
    expect(document.querySelector(".stage")?.className).toBe("stage")
  })
})

describe("ReviewPage: watching the server", () => {
  it.each(["worth", "linkedin"])("never opens the event stream or reads the status on %s", async (stage) => {
    renderPage(`/?stage=${stage}`)
    await waitFor(() => expect(probe(stage)).toBeTruthy())
    act(() => review().syncStatus())
    await Promise.resolve()
    expect(FakeEventSource.opened).toHaveLength(0)
    expect(requests("/api/status")).toHaveLength(0)
  })

  it.each(["enrich", "done"])("opens the stream and reads the status once on %s", async (stage) => {
    server.status = reviewStatus({ stage: stage === "done" ? "done" : "enrich" })
    renderPage(`/?stage=${stage}`)
    await waitFor(() => expect(probe(stage)).toBeTruthy())
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    expect(FakeEventSource.opened.map((source) => source.url)).toEqual(["/api/events"])
  })

  it("closes the stream when the screen changes to one that does not watch", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(probe("enrich")).toBeTruthy())
    fireEvent.click(screen.getByRole("link", { name: /Check LinkedIn/ }))
    await waitFor(() => expect(probe("linkedin")).toBeTruthy())
    expect(FakeEventSource.opened).toHaveLength(1)
    expect(stream().closed).toBe(true)
  })

  it("hands a running job's numbers to the Enrich stage without reading the status", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    act(() => stream().emit({ ...runningEvent(3, 12), replay: true }))
    expect(probeProps("enrich")).toMatchObject({
      job: { status: "running", counts: { total: 12, completed: 3 } },
    })
    act(() => stream().emit(runningEvent(4, 12)))
    expect(probeProps("enrich")).toMatchObject({ job: { counts: { completed: 4 } } })
    expect(requests("/api/status")).toHaveLength(1)
  })

  it("reads the status on every connect, every other event and every unreadable one", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    act(() => stream().open())
    await waitFor(() => expect(requests("/api/status")).toHaveLength(2))
    act(() => stream().emit(changeEvent()))
    await waitFor(() => expect(requests("/api/status")).toHaveLength(3))
    act(() => stream().emit("not json"))
    await waitFor(() => expect(requests("/api/status")).toHaveLength(4))
  })

  it("re-reads the status for a running job on Done: there is no panel to take it", async () => {
    server.status = reviewStatus({ stage: "done" })
    renderPage("/?stage=done")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    act(() => stream().emit(runningEvent()))
    await waitFor(() => expect(requests("/api/status")).toHaveLength(2))
  })

  it("moves forward on a stage change observed while the screen was open", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    server.status = reviewStatus({ stage: "linkedin", state_token: "token-2" })
    act(() => stream().emit(changeEvent()))
    await waitFor(() => expect(screen.getByRole("heading", { name: "Contacts Enriched" })).toBeTruthy())
    await waitFor(() => expect(probe("linkedin")).toBeTruthy(), { timeout: 2000 })
    expect(where()).toBe("/?stage=linkedin")
  })

  it("stays on a screen the server was already past when it opened, and reloads it on a new token", async () => {
    server.status = reviewStatus({ stage: "linkedin", state_token: "token-2" })
    renderPage("/?stage=enrich")
    // The first read is out with the stale token; the read after it carries the server's own,
    // as a real server's page and status always agree.
    expect(requests("/api/review/page")).toHaveLength(1)
    server.pages.enrich = reviewPage("enrich", { state_token: "token-2" })
    await waitFor(() => expect(requests("/api/review/page")).toHaveLength(2))
    await waitFor(() => expect(seen.mounts).toBe(2))
    expect(probe("enrich")).toBeTruthy()
    expect(document.querySelector(".stage-complete")).toBeNull()
    expect(where()).toBe("/?stage=enrich")
  })

  it("never moves a preview screen", async () => {
    renderPage("/?stage=enrich&preview=1")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    server.status = reviewStatus({ stage: "linkedin" })
    act(() => stream().emit(changeEvent()))
    await waitFor(() => expect(requests("/api/status")).toHaveLength(2))
    await Promise.resolve()
    expect(document.querySelector(".stage-complete")).toBeNull()
    expect(probe("enrich")).toBeTruthy()
  })

  it("reads the screen again when the state token changes under it", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    server.status = reviewStatus({ state_token: "token-2" })
    server.pages.enrich = reviewPage("enrich", {
      state_token: "token-2",
      enrichment: enrichmentPanel({ mode: "running", completed: 1, total: 9 }),
    })
    act(() => stream().emit(changeEvent()))
    await waitFor(() => expect(probeProps("enrich")).toMatchObject({ enrichment: { mode: "running" } }))
    expect(seen.mounts).toBe(2)
    expect(FakeEventSource.opened).toHaveLength(2)
  })

  it("does nothing while a stage-complete action is in flight", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    server.status = reviewStatus({ state_token: "token-2" })
    act(() => review().setCompleting(true))
    act(() => stream().emit(changeEvent()))
    await Promise.resolve()
    expect(requests("/api/status")).toHaveLength(1)
    act(() => review().setCompleting(false))
    act(() => review().syncStatus())
    await waitFor(() => expect(requests("/api/review/page")).toHaveLength(2))
  })

  it("never moves or reloads the screen under a typed guidance draft", async () => {
    renderPage("/?stage=enrich")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    server.status = reviewStatus({ stage: "linkedin", state_token: "token-2" })
    act(() => review().setGuidanceDraft(true))
    act(() => stream().emit(changeEvent()))
    await waitFor(() => expect(requests("/api/status")).toHaveLength(2))
    await Promise.resolve()
    expect(document.querySelector(".stage-complete")).toBeNull()
    expect(requests("/api/review/page")).toHaveLength(1)
  })

  it("counts a change only after the stage this screen put the server at", async () => {
    server.status = reviewStatus({ stage: "worth" })
    renderPage("/?stage=enrich&preview=0")
    await waitFor(() => expect(requests("/api/status")).toHaveLength(1))
    act(() => review().noteServerStage("enrich"))
    server.status = reviewStatus({ stage: "enrich" })
    act(() => review().syncStatus())
    await waitFor(() => expect(requests("/api/status")).toHaveLength(2))
    server.status = reviewStatus({ stage: "linkedin" })
    act(() => review().syncStatus())
    await waitFor(() => expect(screen.getByRole("heading", { name: "Contacts Enriched" })).toBeTruthy())
  })
})
