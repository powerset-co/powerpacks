import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import {
  approveResult,
  changeEvent,
  enrichmentPanel,
  errorResponse,
  FakeEventSource,
  jsonResponse,
  motionMedia,
  reviewPage,
  reviewStatus,
  runningEvent,
} from "@/testing/review-fixture"
import type { ReviewPage as ReviewPageData, ReviewStatus } from "@/types/review"

import { ReviewPage } from "../ReviewPage"

// The Enrich stage on the real page: the page's status watcher and event stream drive it.
// The neighbouring stages are other suites' business.
vi.mock("../worth/WorthStage", () => ({ WorthStage: () => <div data-stage-probe="worth" /> }))
vi.mock("../linkedin/LinkedinStage", () => ({ LinkedinStage: () => <div data-stage-probe="linkedin" /> }))

const APPROVE = "/api/review/approve-enrichment"
const PAGE = "/api/review/page"
const STATUS = "/api/status"
const COMPLETE = "/complete"

/** The review server: the Enrich page it serves, its status, and its answer to each POST. */
const server: {
  enrich: ReviewPageData
  status: ReviewStatus
  approve: () => Promise<Response>
  complete: () => Promise<Response>
} = {
  enrich: reviewPage("enrich"),
  status: reviewStatus(),
  approve: () => Promise.resolve(jsonResponse(approveResult())),
  complete: () => Promise.resolve(jsonResponse({ ok: true })),
}

const fetchMock = vi.fn((url: string, _init?: RequestInit): Promise<Response> => {
  const { pathname, searchParams } = new URL(url, "http://review.test")
  if (pathname === STATUS) return Promise.resolve(jsonResponse(server.status))
  if (pathname === APPROVE) return server.approve()
  if (pathname === COMPLETE) return server.complete()
  if (pathname !== PAGE) return Promise.reject(new Error(`unexpected request: ${url}`))
  const stage = searchParams.get("stage")
  return Promise.resolve(jsonResponse(stage === "enrich" ? server.enrich : reviewPage("linkedin")))
})

const requests = (path: string) =>
  fetchMock.mock.calls.map(([url]) => url).filter((url) => url.startsWith(path))
const stream = () => must(FakeEventSource.opened.at(-1), "the event stream")
const heading = () => screen.getByRole("heading", { level: 2 }).textContent
const line = () => document.querySelector(".enrich-state p")?.textContent
const barNow = () => screen.getByRole("progressbar").getAttribute("aria-valuenow")
/** The run's last event: its job is no longer running. */
const finished = () =>
  changeEvent({
    job: { status: "completed", phase: "profiles_complete", counts: { total: 12, completed: 12 } },
  })

function Where() {
  const location = useLocation()
  return <span data-where>{location.pathname + location.search}</span>
}
const where = () => document.querySelector("[data-where]")?.textContent

async function openEnrich(enrichment = enrichmentPanel()) {
  server.enrich = reviewPage("enrich", { enrichment })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/review?stage=enrich"]}>
        <Routes>
          <Route
            path="/review"
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
  await waitFor(() => expect(document.querySelector(".enrich-state")).toBeTruthy())
  await waitFor(() => expect(requests(STATUS)).toHaveLength(1))
}

beforeEach(() => {
  server.status = reviewStatus()
  server.approve = () => Promise.resolve(jsonResponse(approveResult()))
  server.complete = () => Promise.resolve(jsonResponse({ ok: true }))
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

describe("the Enrich screen: live progress (E3, E4)", () => {
  it("resumes the bar from the stream's replay, then follows the run without reading the status", async () => {
    await openEnrich(enrichmentPanel({ mode: "running", completed: 2, total: 12 }))
    expect(line()).toBe("2 of 12 complete")

    act(() => stream().open())
    await waitFor(() => expect(requests(STATUS)).toHaveLength(2))
    act(() => stream().emit({ ...runningEvent(7, 12), seq: 0, replay: true }))
    expect(line()).toBe("7 of 12 complete")
    expect(barNow()).toBe("7")

    act(() => stream().emit(runningEvent(8, 12)))
    expect(line()).toBe("8 of 12 complete")
    expect(barNow()).toBe("8")
    expect(requests(STATUS)).toHaveLength(2)
    expect(requests(PAGE)).toHaveLength(1)
  })

  it("reads the screen again when the run ends on this stage, and resumes from the next replay", async () => {
    await openEnrich(enrichmentPanel({ mode: "running", completed: 11, total: 12 }))
    server.status = reviewStatus({ state_token: "token-2" })
    server.enrich = reviewPage("enrich", {
      state_token: "token-2",
      enrichment: enrichmentPanel({
        mode: "failed",
        error: "enrichment: RuntimeError: judge returned no verdict",
      }),
    })
    act(() => stream().emit(finished()))

    await waitFor(() => expect(heading()).toBe("Enrichment Paused"))
    expect(line()).toBe("enrichment: RuntimeError: judge returned no verdict")
    expect(requests(PAGE)).toHaveLength(2)
    expect(FakeEventSource.opened).toHaveLength(2)
  })

  it("runs the stage check to LinkedIn when the run ends and the server moves on", async () => {
    await openEnrich(enrichmentPanel({ mode: "running", completed: 11, total: 12 }))
    server.status = reviewStatus({
      stage: "linkedin",
      next_action: "review_linkedin",
      state_token: "token-2",
    })
    act(() => stream().emit(finished()))

    await waitFor(() => expect(heading()).toBe("Contacts Enriched"))
    expect(document.querySelector(".stage-complete .empty-mark")?.textContent).toBe("✓")
    await waitFor(() => expect(document.querySelector("[data-stage-probe='linkedin']")).toBeTruthy(), {
      timeout: 2000,
    })
    expect(where()).toBe("/review?stage=linkedin")
  })
})

describe("the Enrich screen: approve (E2, P0.2)", () => {
  it("approves once, in place, and follows the run it started", async () => {
    await openEnrich()
    const button = screen.getByRole("button", { name: "Approve $2.85" })
    fireEvent.click(button)
    fireEvent.click(button)

    await waitFor(() => expect(heading()).toBe("Enriching Contacts"))
    expect(requests(APPROVE)).toHaveLength(1)
    expect(line()).toBe("0 of 12 complete")
    expect(screen.getByRole("status").textContent).toBe("Approved")
    expect(screen.queryByRole("button")).toBeNull()
    // In place: the page was not read again, the stream stayed open, the status was not re-read.
    expect(requests(PAGE)).toHaveLength(1)
    expect(FakeEventSource.opened).toHaveLength(1)
    expect(requests(STATUS)).toHaveLength(1)

    act(() => stream().emit(runningEvent(5, 12)))
    expect(line()).toBe("5 of 12 complete")
    expect(barNow()).toBe("5")
  })

  it("holds the status watcher while the POST is in flight, then re-reads for an answer that is not running", async () => {
    let answer: (response: Response) => void = () => undefined
    server.approve = () =>
      new Promise<Response>((resolve) => {
        answer = resolve
      })
    await openEnrich()
    fireEvent.click(screen.getByRole("button", { name: "Approve $2.85" }))
    await waitFor(() => expect(requests(APPROVE)).toHaveLength(1))

    server.status = reviewStatus({ state_token: "token-2" })
    act(() => stream().emit(changeEvent()))
    await Promise.resolve()
    expect(requests(STATUS)).toHaveLength(1)
    expect(requests(PAGE)).toHaveLength(1)

    server.status = reviewStatus()
    answer(jsonResponse(approveResult({ mode: "completed" })))
    await waitFor(() => expect(heading()).toBe("Contacts Enriched"))
    await waitFor(() => expect(requests(STATUS)).toHaveLength(2))
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Continue" }).disabled).toBe(false)
    expect(requests(PAGE)).toHaveLength(1)
  })

  it("shows a refusal in the toast and keeps the approve button", async () => {
    server.approve = () => Promise.resolve(errorResponse("enrichment job execution is disabled", 409))
    await openEnrich()
    fireEvent.click(screen.getByRole("button", { name: "Approve $2.85" }))

    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toBe("enrichment job execution is disabled"),
    )
    expect(heading()).toBe("Ready to Enrich")
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Approve $2.85" }).disabled).toBe(false)
  })
})

describe("the Enrich screen: Continue (E5)", () => {
  it("marks the stage complete, shows the check, then opens LinkedIn", async () => {
    await openEnrich(enrichmentPanel({ mode: "completed" }))
    fireEvent.click(screen.getByRole("button", { name: "Continue" }))

    await waitFor(() => expect(document.querySelector(".stage-complete")).toBeTruthy())
    expect(heading()).toBe("Contacts Enriched")
    expect(requests(COMPLETE)).toHaveLength(1)
    expect(document.querySelector(".enrich-state")).toBeNull()
    await waitFor(() => expect(document.querySelector("[data-stage-probe='linkedin']")).toBeTruthy(), {
      timeout: 2000,
    })
    expect(where()).toBe("/review?stage=linkedin")
  })
})
