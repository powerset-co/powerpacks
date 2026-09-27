// The results toolbar's filter over a table's rows (results.js filteredRows): tagged only
// (and which tags), overall score, operators, plus the labels toggle and the count.

import { readSession, writeSession } from "@/lib/storage"
import type { Candidate, PondCandidate, Tagged } from "@/types/searches"

const LABELS_KEY = "powerpacks:search-labels"

export const SCORES = [1, 2, 3, 4, 5] as const
export type Score = (typeof SCORES)[number]

/** The fields of a table row the toolbar reads; the run view's ResultRow
 *  (pages/searches/lib/ranking.ts) is one. */
export interface ToolbarRow {
  row: PondCandidate
  candidate: Candidate | undefined
  // The judges' overall, or the screen outcome below 3; null in a pond table.
  overall: number | null
  reason: string
}

export interface ResultFilters {
  taggedOnly: boolean
  // Within tagged only: people holding any of these tags; empty means any tag.
  tags: ReadonlySet<string>
  scores: ReadonlySet<Score>
  operators: ReadonlySet<string>
  labels: boolean
}

export const NO_FILTERS: ResultFilters = {
  taggedOnly: false,
  tags: new Set(),
  scores: new Set(),
  operators: new Set(),
  labels: true,
}

export interface OperatorOption {
  id: string
  name: string
}

/** The score export reads: the person's own score when saved, else the overall. */
export function exportScore(row: ToolbarRow): number | null {
  return row.candidate?.human_score ?? row.overall
}

function operatorsOf(row: ToolbarRow) {
  return row.candidate?.network_attribution?.operators ?? []
}

function tagsOf(tagged: Tagged, row: ToolbarRow): readonly string[] {
  return tagged.assignments[row.row.person_id] ?? []
}

/** People in `rows` holding at least one tag. */
export function taggedCount(rows: readonly ToolbarRow[], tagged: Tagged): number {
  return new Set(rows.filter((row) => tagsOf(tagged, row).length).map((row) => row.row.person_id)).size
}

/** The filters as they apply: tagged only lapses when nobody is tagged, and tag filters
 *  drop tags the search no longer has. */
export function liveFilters(
  filters: ResultFilters,
  rows: readonly ToolbarRow[],
  tagged: Tagged,
): ResultFilters {
  const tags = new Set([...filters.tags].filter((tag) => tagged.tags.includes(tag)))
  return { ...filters, taggedOnly: filters.taggedOnly && taggedCount(rows, tagged) > 0, tags }
}

function matches(
  row: ToolbarRow,
  filters: ResultFilters,
  tagged: Tagged,
  scoreOf: (row: ToolbarRow) => number | null,
): boolean {
  const tags = tagsOf(tagged, row)
  const score = scoreOf(row)
  const tagOk =
    !filters.taggedOnly ||
    (tags.length > 0 && (!filters.tags.size || tags.some((tag) => filters.tags.has(tag))))
  const scoreOk = !filters.scores.size || SCORES.some((held) => held === score && filters.scores.has(held))
  const operatorOk =
    !filters.operators.size ||
    operatorsOf(row).some((operator) => filters.operators.has(operator.operator_id))
  return tagOk && scoreOk && operatorOk
}

/**
 * The rows the filters keep, one per person (the first: the run view ranks best first).
 * The table filters on the model's overall; export passes `exportScore`, so a person's own
 * score decides what leaves the page without moving the table.
 */
export function filterRows(
  rows: readonly ToolbarRow[],
  filters: ResultFilters,
  tagged: Tagged,
  scoreOf: (row: ToolbarRow) => number | null = (row) => row.overall,
): ToolbarRow[] {
  const live = liveFilters(filters, rows, tagged)
  const kept = new Map<string, ToolbarRow>()
  for (const row of rows) {
    if (!matches(row, live, tagged, scoreOf)) continue
    if (!kept.has(row.row.person_id)) kept.set(row.row.person_id, row)
  }
  return [...kept.values()]
}

/** "12 results", or "12 of 125 results" while a filter hides some. */
export function countText(shown: number, total: number): string {
  const noun = total === 1 ? "result" : "results"
  return shown === total
    ? `${total.toLocaleString()} ${noun}`
    : `${shown.toLocaleString()} of ${total.toLocaleString()} ${noun}`
}

/** Everyone connected to someone in `rows`, by name (rendering.py _results_toolbar). */
export function operatorOptions(rows: readonly ToolbarRow[]): OperatorOption[] {
  const byId = new Map<string, string>()
  for (const row of rows) {
    for (const operator of operatorsOf(row)) byId.set(operator.operator_id, operator.operator_name)
  }
  return [...byId]
    .map(([id, name]) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }))
}

/** Whether labels (Taste, Suggested pin, Team similarity) show; per tab, like results.js. */
export function labelsShown(): boolean {
  return readSession(LABELS_KEY, (raw) => (typeof raw === "boolean" ? raw : null)) ?? true
}

export function saveLabels(shown: boolean): void {
  writeSession(LABELS_KEY, shown)
}
