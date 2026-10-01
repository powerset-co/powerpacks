import { describe, expect, it } from "vitest"

import type { EnrichmentJob } from "@/lib/review/sync"
import { enrichmentPanel } from "@/testing/review-fixture"

import { barPercent, jobProgress, panelProgress } from "./progress"

const SHOWN = { text: "3 of 12 complete", completed: 3, total: 12 }

function judging(done?: number, total?: number): EnrichmentJob {
  return {
    status: "running",
    phase: "judging_retargets",
    // The pipeline writes the judge's count into the job's own counts during this phase.
    counts: { total: 12, completed: done },
    progress: { phase_done: done, phase_total: total },
  }
}

describe("panelProgress", () => {
  it("is the server's count and its line", () => {
    expect(panelProgress(enrichmentPanel({ mode: "running", completed: 3, total: 12 }))).toEqual(SHOWN)
    expect(panelProgress(enrichmentPanel({ mode: "running" })).text).toBe("0 of 0 complete")
  })
})

describe("jobProgress", () => {
  it("rewrites the line and the bar from a research event", () => {
    const job: EnrichmentJob = { status: "running", phase: "research", counts: { total: 12, completed: 7 } }
    expect(jobProgress(SHOWN, job)).toEqual({ text: "7 of 12 complete", completed: 7, total: 12 })
  })

  it("takes a new total with the count", () => {
    const job: EnrichmentJob = { status: "running", counts: { total: 20, completed: 4 } }
    expect(jobProgress(SHOWN, job)).toEqual({ text: "4 of 20 complete", completed: 4, total: 20 })
  })

  it("never counts past the total", () => {
    const job: EnrichmentJob = { status: "running", counts: { total: 12, completed: 15 } }
    expect(jobProgress(SHOWN, job)).toEqual({ text: "12 of 12 complete", completed: 12, total: 12 })
  })

  it("says what the judge has checked and leaves the bar where it was", () => {
    expect(jobProgress(SHOWN, judging(2, 5))).toEqual({ text: "2 of 5 checked", completed: 3, total: 12 })
  })

  it("reads a judging event without its numbers as none checked", () => {
    expect(jobProgress(SHOWN, judging())).toEqual({ text: "0 of 0 checked", completed: 3, total: 12 })
    const bare: EnrichmentJob = { status: "running", phase: "judging_retargets", counts: {} }
    expect(jobProgress(SHOWN, bare).text).toBe("0 of 0 checked")
  })

  it("leaves the bar where it was when the job has nothing planned", () => {
    const empty: EnrichmentJob = { status: "running", counts: { total: 0, completed: 0 } }
    expect(jobProgress(SHOWN, empty)).toEqual({ text: "0 of 0 complete", completed: 3, total: 12 })
    expect(jobProgress(SHOWN, { status: "running", counts: {} }).text).toBe("0 of 0 complete")
  })
})

describe("barPercent", () => {
  it("is the whole percent finished, rounded half up", () => {
    expect(barPercent({ ...SHOWN, completed: 3, total: 12 })).toBe(25)
    expect(barPercent({ ...SHOWN, completed: 1, total: 3 })).toBe(33)
    expect(barPercent({ ...SHOWN, completed: 1, total: 8 })).toBe(13)
    expect(barPercent({ ...SHOWN, completed: 12, total: 12 })).toBe(100)
  })

  it("is 0 with nothing planned", () => {
    expect(barPercent({ ...SHOWN, completed: 0, total: 0 })).toBe(0)
  })
})
