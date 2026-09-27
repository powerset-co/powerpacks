// Which people a run shows and in what order: rendering.py _cross_encoder_table,
// _overall_score, _is_qualification_score and _pond_table, rule for rule. Qualification
// (Jev) and rating scales are never compared; a missing score is never a zero.

import type { Candidate, Pond, PondCandidate, SearchResult } from "@/types/searches"

// human_ratings.py QUALIFICATION_SCORE_TYPE.
const QUALIFICATION_SCORE_TYPE = "qualification_score"
const SECTION_HEADINGS = { qualification: "Jev qualification scores", rating: "Rating-based scores" } as const

export type ScoreBand = "high" | "medium" | "low"

/** rendering.py _score_band, on a 0–1 scale. */
export function scoreBand(score: number): ScoreBand {
  if (score >= 0.8) return "high"
  return score >= 0.5 ? "medium" : "low"
}

export function isQualificationScore(row: PondCandidate): boolean {
  return row.cross_encoder_score_type === QUALIFICATION_SCORE_TYPE
}

/** The judges' overall; else a rating screen below 3 as 1 or 2; else none. */
export function overallScore(row: PondCandidate, candidate: Candidate | undefined): number | null {
  const judged = candidate?.candidate_judgment?.overall_score
  if (judged !== undefined && judged !== null) return judged
  if (isQualificationScore(row)) return null
  const ce = row.cross_encoder_score_1_to_5
  if (ce !== null && ce < 3) return ce >= 2 ? 2 : 1
  return null
}

/** The screen's own number on its own scale; a missing one sorts last. */
function ceScore(row: PondCandidate): number {
  const value = isQualificationScore(row) ? row.cross_encoder_score : row.cross_encoder_score_1_to_5
  return value ?? Number.NEGATIVE_INFINITY
}

/** The line beside the score: the judges' deciding reason, or what the screen did. */
function overallReason(row: PondCandidate, candidate: Candidate | undefined, overall: number | null): string {
  const judgment = candidate?.candidate_judgment
  if (judgment && judgment.overall_score !== null) {
    const capped =
      judgment.opportunity_cap !== null &&
      judgment.domain_score !== null &&
      judgment.opportunity_cap < judgment.domain_score
    return capped ? judgment.opportunity_reason : judgment.domain_reason
  }
  if (overall !== null || row.cross_encoder_passed === false) return "Did not pass screen"
  return "Not judged"
}

export interface ResultRow {
  key: string
  row: PondCandidate
  candidate: Candidate | undefined
  // The overall 1–5 score; null means none, shown as the reason, never as 0.
  overall: number | null
  reason: string
}

export interface ResultSection {
  // Set only when a run mixes the two scales and shows them as two tables.
  heading: string | null
  rows: ResultRow[]
}

export type Results =
  // Screened people, deduplicated by their best screen score, ranked by overall.
  | { mode: "ranked"; sections: ResultSection[] }
  // The screen ran and scored no one.
  | { mode: "unavailable" }
  // No screen ran: one table per pond, picked from the pond chain.
  | { mode: "ponds" }

// Python's sorted(reverse=True): descending, equal keys keep their order (Array.sort is stable).
function descending<T>(items: readonly T[], key: (item: T) => readonly number[]): T[] {
  return [...items].sort((a, b) => {
    const left = key(a)
    const right = key(b)
    for (const [position, value] of left.entries()) {
      const other = right[position] ?? value
      if (value !== other) return value < other ? 1 : -1
    }
    return 0
  })
}

function candidatesById(search: SearchResult): Map<string, Candidate> {
  const byId = new Map<string, Candidate>()
  for (const candidate of search.candidates) {
    if (!byId.has(candidate.person_id)) byId.set(candidate.person_id, candidate)
  }
  return byId
}

function rankedSection(
  rows: readonly PondCandidate[],
  byId: ReadonlyMap<string, Candidate>,
  heading: string | null,
  section: number,
): ResultSection {
  const best = new Map<string, PondCandidate>()
  for (const row of descending(rows, (item) => [ceScore(item)])) {
    if (!best.has(row.person_id)) best.set(row.person_id, row)
  }
  const ranked = descending([...best.values()], (row) => [
    overallScore(row, byId.get(row.person_id)) ?? 0,
    ceScore(row),
  ])
  return {
    heading,
    rows: ranked.map((row) => {
      const candidate = byId.get(row.person_id)
      const overall = overallScore(row, candidate)
      return {
        key: `${section}:${row.person_id}`,
        row,
        candidate,
        overall,
        reason: overallReason(row, candidate, overall),
      }
    }),
  }
}

export function rankResults(search: SearchResult): Results {
  const byId = candidatesById(search)
  const everyRow = search.ponds.flatMap((pond) => pond.candidates)
  const screened = everyRow.filter((row) => row.cross_encoder_score !== null)
  if (!screened.length) {
    return everyRow.some((row) => row.cross_encoder_status) ? { mode: "unavailable" } : { mode: "ponds" }
  }
  const qualification = screened.filter(isQualificationScore)
  const rating = screened.filter((row) => !isQualificationScore(row))
  if (qualification.length && rating.length) {
    return {
      mode: "ranked",
      sections: [
        rankedSection(qualification, byId, SECTION_HEADINGS.qualification, 0),
        rankedSection(rating, byId, SECTION_HEADINGS.rating, 1),
      ],
    }
  }
  return { mode: "ranked", sections: [rankedSection(screened, byId, null, 0)] }
}

/** rendering.py _pond_table: one pond's people by their pond score, reasoning beside it. */
export function pondRows(search: SearchResult, pond: Pond): ResultRow[] {
  const byId = candidatesById(search)
  return descending(pond.candidates, (row) => [row.final_score]).map((row) => ({
    key: `${pond.run_id}:${pond.pond_n}:${row.person_id}`,
    row,
    candidate: byId.get(row.person_id),
    overall: null,
    reason: row.reasoning,
  }))
}

/** How many people a pond kept: those reviewed, else the rows it returned. */
export function pondKept(pond: Pond): number {
  return pond.reviewed_count || pond.candidates.length
}
