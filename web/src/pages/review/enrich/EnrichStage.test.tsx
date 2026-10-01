import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { must } from "@/lib/must"
import type { EnrichmentJob } from "@/lib/review/sync"
import {
  approveResult,
  enrichmentPanel,
  errorResponse,
  fakeReview,
  jsonResponse,
  runningEvent,
} from "@/testing/review-fixture"
import { ReviewHarness } from "@/testing/review-harness"
import type { EnrichmentPanel } from "@/types/review"

import { EnrichStage } from "./EnrichStage"

const APPROVE = "/api/review/approve-enrichment"
const COMPLETE = "/complete"

const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>()

/** The requests the stage sent: path, method and form body. */
const posts = () =>
  fetchMock.mock.calls.map(([url, init]) => ({
    url,
    method: init?.method,
    body: init?.body instanceof URLSearchParams ? init.body.toString() : null,
  }))

/** A request the test answers when it chooses. */
function heldRequest() {
  let answer: (response: Response) => void = () => undefined
  const response = new Promise<Response>((resolve) => {
    answer = resolve
  })
  fetchMock.mockReturnValueOnce(response)
  return (with_: Response) => answer(with_)
}

function watchedReview() {
  const watch = {
    toast: vi.fn(),
    toastError: vi.fn(),
    transition: vi.fn(),
    reload: vi.fn(),
    leaveAndReload: vi.fn(),
    syncStatus: vi.fn(),
    noteServerStage: vi.fn(),
    setCompleting: vi.fn(),
  }
  return { watch, review: fakeReview(watch) }
}

function renderStage(enrichment: EnrichmentPanel, job: EnrichmentJob | null = null) {
  const { watch, review } = watchedReview()
  const stage = (latest: EnrichmentJob | null) => (
    <ReviewHarness review={review}>
      <EnrichStage enrichment={enrichment} job={latest} />
    </ReviewHarness>
  )
  const view = render(stage(job))
  /** The next /api/events job, as the page hands it down. */
  const hear = (latest: EnrichmentJob) => view.rerender(stage(latest))
  return { ...view, watch, hear }
}

const running = (completed: number, total: number) => must(runningEvent(completed, total).job)
const judging = (done: number, total: number): EnrichmentJob => ({
  status: "running",
  phase: "judging_retargets",
  counts: { total: 12, completed: done },
  progress: { phase_done: done, phase_total: total },
})

const panel = (container: HTMLElement) => must(container.firstElementChild, "the panel")
const heading = () => screen.getByRole("heading", { level: 2 }).textContent
const bar = () => {
  const element = screen.getByRole("progressbar")
  const fill = must(element.querySelector<HTMLElement>(".enrich-progress-fill"), "the fill")
  return {
    min: element.getAttribute("aria-valuemin"),
    max: element.getAttribute("aria-valuemax"),
    now: element.getAttribute("aria-valuenow"),
    width: fill.style.width,
  }
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal("fetch", fetchMock)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("EnrichStage: the panel's five states (E1)", () => {
  it("says it is preparing, with nothing under the title", () => {
    const { container } = renderStage(enrichmentPanel({ mode: "preparing" }))
    expect(panel(container).className).toBe("empty-state enrich-state")
    expect(panel(container).textContent).toBe("Preparing Enrichment")
    expect(panel(container).children).toHaveLength(1)
  })

  it("offers the one approve button with the estimate", () => {
    const { container } = renderStage(enrichmentPanel({ mode: "approval", approval_label: "Approve $2.85" }))
    expect(panel(container).className).toBe("empty-state enrich-state")
    expect(heading()).toBe("Ready to Enrich")
    const buttons = screen.getAllByRole<HTMLButtonElement>("button")
    expect(buttons.map((button) => button.textContent)).toEqual(["Approve $2.85"])
    expect(buttons[0]?.className).toBe("button button-primary button-hero")
    expect(buttons[0]?.disabled).toBe(false)
  })

  it("words the button for a run that spends nothing", () => {
    renderStage(enrichmentPanel({ mode: "approval", approval_label: "Prepare profiles and judge LinkedIns" }))
    expect(heading()).toBe("Ready to Enrich")
    expect(screen.getByRole("button").textContent).toBe("Prepare profiles and judge LinkedIns")
  })

  it("shows a running enrichment's count and bar, and no button", () => {
    const { container } = renderStage(enrichmentPanel({ mode: "running", completed: 3, total: 12 }))
    expect(panel(container).className).toBe("empty-state enrich-state")
    expect(heading()).toBe("Enriching Contacts")
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("3 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "3", width: "25%" })
    expect(screen.getByRole("progressbar").className).toBe("enrich-progress")
    expect(screen.queryByRole("button")).toBeNull()
  })

  it("draws an empty bar when nothing is planned", () => {
    const { container } = renderStage(enrichmentPanel({ mode: "running", completed: 0, total: 0 }))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("0 of 0 complete")
    expect(bar()).toEqual({ min: "0", max: "0", now: "0", width: "0%" })
  })

  it("offers Continue once the contacts are enriched", () => {
    const { container } = renderStage(enrichmentPanel({ mode: "completed" }))
    expect(panel(container).className).toBe("empty-state enrich-state")
    expect(heading()).toBe("Contacts Enriched")
    const buttons = screen.getAllByRole("button")
    expect(buttons.map((button) => button.textContent)).toEqual(["Continue"])
    expect(buttons[0]?.className).toBe("button button-primary")
  })

  it("shows the error of a paused enrichment, and no button", () => {
    const error = "enrichment: RuntimeError: research stopped with status failed"
    const { container } = renderStage(enrichmentPanel({ mode: "failed", error }))
    expect(panel(container).className).toBe("empty-state enrich-state")
    expect(heading()).toBe("Enrichment Paused")
    expect(container.querySelector(".enrich-state p")?.textContent).toBe(error)
    expect(screen.queryByRole("button")).toBeNull()
  })
})

describe("EnrichStage: approve (E2, P0.2)", () => {
  it("posts once under a double click and holds the button while the POST is in flight", async () => {
    const answer = heldRequest()
    const { watch } = renderStage(enrichmentPanel())
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Approve $2.85" })
    fireEvent.click(button)
    fireEvent.click(button)

    expect(posts()).toEqual([{ url: APPROVE, method: "POST", body: "" }])
    expect(screen.getAllByRole<HTMLButtonElement>("button").map((each) => each.disabled)).toEqual([true])
    expect(button.getAttribute("aria-busy")).toBe("true")
    expect(watch.setCompleting.mock.calls).toEqual([[true]])
    expect(heading()).toBe("Ready to Enrich")

    answer(jsonResponse(approveResult()))
    await waitFor(() => expect(heading()).toBe("Enriching Contacts"))
    expect(posts()).toHaveLength(1)
  })

  it("becomes the answer's running panel in place: no reload, and the status is not read", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(approveResult({ completed: 1, total: 12 })))
    const { container, watch } = renderStage(enrichmentPanel())
    fireEvent.click(screen.getByRole("button", { name: "Approve $2.85" }))

    await waitFor(() => expect(heading()).toBe("Enriching Contacts"))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("1 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "1", width: "8%" })
    expect(screen.queryByRole("button")).toBeNull()
    expect(watch.setCompleting.mock.calls).toEqual([[true], [false]])
    expect(watch.noteServerStage.mock.calls).toEqual([["enrich"]])
    expect(watch.toast.mock.calls).toEqual([["Approved"]])
    expect(watch.toastError).not.toHaveBeenCalled()
    expect(watch.syncStatus).not.toHaveBeenCalled()
    expect(watch.reload).not.toHaveBeenCalled()
    expect(watch.leaveAndReload).not.toHaveBeenCalled()
  })

  it("shows the answer's panel and re-reads the status when the answer is not running", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(approveResult({ mode: "completed" })))
    const { watch } = renderStage(enrichmentPanel())
    fireEvent.click(screen.getByRole("button", { name: "Approve $2.85" }))

    await waitFor(() => expect(heading()).toBe("Contacts Enriched"))
    const next = screen.getByRole<HTMLButtonElement>("button", { name: "Continue" })
    expect(next.disabled).toBe(false)
    expect(next.getAttribute("aria-busy")).toBeNull()
    expect(watch.toast.mock.calls).toEqual([["Approved"]])
    expect(watch.noteServerStage.mock.calls).toEqual([["enrich"]])
    expect(watch.setCompleting.mock.calls).toEqual([[true], [false]])
    expect(watch.syncStatus).toHaveBeenCalledTimes(1)
    // The watcher was let go before it was asked to read.
    expect(must(watch.setCompleting.mock.invocationCallOrder[1])).toBeLessThan(
      must(watch.syncStatus.mock.invocationCallOrder[0]),
    )
  })

  it("re-enables the button and shows the server's words when the POST is refused", async () => {
    fetchMock.mockResolvedValueOnce(errorResponse("enrichment job execution is disabled", 409))
    const { watch } = renderStage(enrichmentPanel())
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Approve $2.85" })
    fireEvent.click(button)

    await waitFor(() =>
      expect(watch.toastError.mock.calls).toEqual([["enrichment job execution is disabled"]]),
    )
    expect(heading()).toBe("Ready to Enrich")
    expect(button.disabled).toBe(false)
    expect(button.getAttribute("aria-busy")).toBeNull()
    expect(watch.setCompleting.mock.calls).toEqual([[true], [false]])
    expect(watch.toast).not.toHaveBeenCalled()
    expect(watch.noteServerStage).not.toHaveBeenCalled()
    expect(watch.syncStatus).not.toHaveBeenCalled()

    fetchMock.mockResolvedValueOnce(jsonResponse(approveResult()))
    fireEvent.click(button)
    await waitFor(() => expect(heading()).toBe("Enriching Contacts"))
    expect(posts().map((post) => post.url)).toEqual([APPROVE, APPROVE])
  })

  it("re-enables the button when the request never reaches the server", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"))
    const { watch } = renderStage(enrichmentPanel())
    fireEvent.click(screen.getByRole("button", { name: "Approve $2.85" }))

    await waitFor(() => expect(watch.toastError.mock.calls).toEqual([["Failed to fetch"]]))
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Approve $2.85" }).disabled).toBe(false)
    expect(watch.setCompleting.mock.calls).toEqual([[true], [false]])
  })
})

describe("EnrichStage: live progress (E3)", () => {
  it("rewrites the count and the bar in place from each running job", () => {
    const { container, hear } = renderStage(enrichmentPanel({ mode: "running", completed: 3, total: 12 }))
    const element = screen.getByRole("progressbar")
    hear(running(4, 12))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("4 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "4", width: "33%" })
    hear(running(9, 12))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("9 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "9", width: "75%" })
    expect(screen.getByRole("progressbar")).toBe(element)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("resumes the bar from the job the stream replays after a fresh mount", () => {
    const { container, hear } = renderStage(enrichmentPanel({ mode: "running", completed: 0, total: 12 }))
    expect(bar()).toEqual({ min: "0", max: "12", now: "0", width: "0%" })
    hear(running(7, 12))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("7 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "7", width: "58%" })
  })

  it("says what the judge has checked and leaves the bar alone", () => {
    const { container, hear } = renderStage(enrichmentPanel({ mode: "running", completed: 3, total: 12 }))
    hear(running(12, 12))
    expect(bar()).toEqual({ min: "0", max: "12", now: "12", width: "100%" })
    hear(judging(2, 5))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("2 of 5 checked")
    expect(bar()).toEqual({ min: "0", max: "12", now: "12", width: "100%" })
    hear(judging(3, 5))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("3 of 5 checked")
    expect(bar()).toEqual({ min: "0", max: "12", now: "12", width: "100%" })
  })

  it("leaves the bar as the page drew it when the first job heard is judging", () => {
    const { container, hear } = renderStage(enrichmentPanel({ mode: "running", completed: 1, total: 12 }))
    hear(judging(1, 5))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("1 of 5 checked")
    expect(bar()).toEqual({ min: "0", max: "12", now: "1", width: "8%" })
  })

  it("never counts past the total", () => {
    const { container, hear } = renderStage(enrichmentPanel({ mode: "running", completed: 3, total: 12 }))
    hear(running(15, 12))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("12 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "12", width: "100%" })
  })

  it("changes nothing on a panel that is not running", () => {
    const { container, hear } = renderStage(enrichmentPanel())
    const before = container.innerHTML
    hear(running(4, 12))
    expect(container.innerHTML).toBe(before)
    expect(screen.queryByRole("progressbar")).toBeNull()
  })

  it("draws the approve answer's numbers over a job heard before it, then follows the next job", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(approveResult({ completed: 6, total: 12 })))
    const { container, hear } = renderStage(enrichmentPanel())
    hear(running(5, 12))
    fireEvent.click(screen.getByRole("button", { name: "Approve $2.85" }))

    await waitFor(() => expect(heading()).toBe("Enriching Contacts"))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("6 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "6", width: "50%" })
    hear(running(8, 12))
    expect(container.querySelector(".enrich-state p")?.textContent).toBe("8 of 12 complete")
    expect(bar()).toEqual({ min: "0", max: "12", now: "8", width: "67%" })
  })
})

describe("EnrichStage: Continue (E5)", () => {
  it("posts the stage complete once, then runs the stage check to LinkedIn", async () => {
    const answer = heldRequest()
    const { watch } = renderStage(enrichmentPanel({ mode: "completed" }))
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Continue" })
    fireEvent.click(button)
    fireEvent.click(button)

    expect(posts()).toEqual([{ url: COMPLETE, method: "POST", body: "stage=enrich" }])
    expect(button.disabled).toBe(true)
    expect(button.getAttribute("aria-busy")).toBe("true")
    expect(watch.setCompleting.mock.calls).toEqual([[true]])
    expect(watch.transition).not.toHaveBeenCalled()

    answer(jsonResponse({ ok: true }))
    await waitFor(() => expect(watch.transition.mock.calls).toEqual([["Contacts Enriched", "linkedin"]]))
    expect(posts()).toHaveLength(1)
    // The watcher stays held until the next screen loads.
    expect(watch.setCompleting.mock.calls).toEqual([[true]])
    expect(watch.toastError).not.toHaveBeenCalled()
  })

  it("re-enables Continue and shows the server's words when the POST fails", async () => {
    fetchMock.mockResolvedValueOnce(new Response("stage enrich is not ready to complete", { status: 409 }))
    const { watch } = renderStage(enrichmentPanel({ mode: "completed" }))
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Continue" })
    fireEvent.click(button)

    await waitFor(() =>
      expect(watch.toastError.mock.calls).toEqual([["stage enrich is not ready to complete"]]),
    )
    expect(heading()).toBe("Contacts Enriched")
    expect(button.disabled).toBe(false)
    expect(button.getAttribute("aria-busy")).toBeNull()
    expect(watch.setCompleting.mock.calls).toEqual([[true], [false]])
    expect(watch.transition).not.toHaveBeenCalled()

    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }))
    fireEvent.click(button)
    await waitFor(() => expect(watch.transition).toHaveBeenCalledTimes(1))
    expect(posts().map((post) => post.url)).toEqual([COMPLETE, COMPLETE])
  })
})
