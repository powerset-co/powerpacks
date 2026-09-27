import { describe, expect, it } from "vitest"

import { RANKED_NAMES, RUN } from "@/testing/searches-fixture"
import type { PondCandidate, SearchResult } from "@/types/searches"

import { overallScore, pondRows, rankResults } from "./ranking"

const { search } = RUN

function names(results: ReturnType<typeof rankResults>): string[][] {
  if (results.mode !== "ranked") throw new Error(`expected ranked, got ${results.mode}`)
  return results.sections.map((section) => section.rows.map((result) => result.row.name))
}

function withRows(change: (row: PondCandidate) => PondCandidate): SearchResult {
  return {
    ...search,
    ponds: search.ponds.map((pond) => ({ ...pond, candidates: pond.candidates.map(change) })),
  }
}

describe("rankResults", () => {
  it("never compares the two scales: Jev first, then ratings, each by overall then screen score", () => {
    const results = rankResults(search)
    expect(names(results)).toEqual(RANKED_NAMES)
    if (results.mode !== "ranked") return
    expect(results.sections.map((section) => section.heading)).toEqual([
      "Jev qualification scores",
      "Rating-based scores",
    ])
  })

  it("keeps a person's best-screened row once, and says why an unscored person has no overall", () => {
    const results = rankResults(search)
    if (results.mode !== "ranked") throw new Error("expected ranked")
    const [jev, rating] = results.sections
    const casey = rating?.rows.find((result) => result.row.name === "Casey Delta")
    expect(rating?.rows.filter((result) => result.row.name === "Casey Delta")).toHaveLength(1)
    expect(casey?.row.cross_encoder_score).toBe(4.1)
    expect([casey?.overall, casey?.reason]).toEqual([null, "Not judged"])
    const morgan = rating?.rows.find((result) => result.row.name === "Morgan Echo")
    expect([morgan?.overall, morgan?.reason]).toEqual([2, "Did not pass screen"])
    const quinn = jev?.rows.find((result) => result.row.name === "Quinn Hotel")
    expect([quinn?.overall, quinn?.reason]).toEqual([null, "Did not pass screen"])
    expect(rating?.rows[0]?.reason).toBe("Built this exact system twice.")
  })

  it("a zero screen score is a score; a missing one is not", () => {
    const zero = withRows((row) => (row.name === "Casey Delta" ? { ...row, cross_encoder_score: 0 } : row))
    expect(names(rankResults(zero))[1]).toContain("Casey Delta")
    const missing = withRows((row) =>
      row.name === "Casey Delta" ? { ...row, cross_encoder_score: null } : row,
    )
    expect(names(rankResults(missing)).flat()).not.toContain("Casey Delta")
  })

  it("a rating below 3 is shown as 1 or 2; 3 and up, and every Jev score, have no overall", () => {
    const [row] = search.ponds[0]?.candidates ?? []
    if (!row) throw new Error("fixture has no rows")
    const cases: [number, number | null][] = [
      [1.0, 1],
      [1.99, 1],
      [2.0, 2],
      [2.99, 2],
      [3.0, null],
    ]
    for (const [ce, expected] of cases) {
      expect(overallScore({ ...row, cross_encoder_score_1_to_5: ce }, undefined)).toBe(expected)
    }
    const jev = { ...row, cross_encoder_score_type: "qualification_score", cross_encoder_score_1_to_5: 1.2 }
    expect(overallScore(jev, undefined)).toBeNull()
  })

  it("no screen at all falls back to one table per pond; a failed screen says so", () => {
    const unscreened = withRows((row) => ({ ...row, cross_encoder_score: null, cross_encoder_status: "" }))
    expect(rankResults(unscreened).mode).toBe("ponds")
    const failed = withRows((row) => ({ ...row, cross_encoder_score: null, cross_encoder_status: "failed" }))
    expect(rankResults(failed).mode).toBe("unavailable")
  })
})

describe("pondRows", () => {
  it("orders one pond by its own score", () => {
    const pond = search.ponds[0]
    if (!pond) throw new Error("fixture has no ponds")
    const scored = {
      ...pond,
      candidates: pond.candidates.map((row, index) => ({
        ...row,
        final_score: [0.2, 0.9, 0.5, 0.7][index] ?? 0,
      })),
    }
    expect(pondRows(search, scored).map((result) => result.row.name)).toEqual([
      "Casey Delta",
      "Riley Foxtrot",
      "Morgan Echo",
      "Jordan Bravo",
    ])
  })
})
