import { describe, expect, it } from "vitest"

import { operator, resultRow } from "@/testing/searches-fixture"
import type { Tagged } from "@/types/searches"

import { exportScoreOf } from "./feedback"
import {
  countText,
  filterRows,
  keptRows,
  liveFilters,
  NO_FILTERS,
  operatorOptions,
  taggedCount,
  type ResultFilters,
  type Score,
} from "./filters"
import { NO_TAGS } from "./tags"

const DREW = operator("op-1", "Drew Kilo")
const EMERY = operator("op-2", "emery Lima")

// test_browser_overall_filters_export_all_matching_rows_and_tags: 125 people, overall 1–5 in turn.
const SCORED = Array.from({ length: 125 }, (_, index) =>
  resultRow(`score-person-${index}`, `Person ${index}`, { overall: (index % 5) + 1 }),
)

function only(patch: Partial<ResultFilters>): ResultFilters {
  return { ...NO_FILTERS, ...patch }
}

function ids(rows: readonly { row: { person_id: string } }[]): string[] {
  return rows.map((row) => row.row.person_id)
}

describe("filterRows", () => {
  it("keeps every row with no filter", () => {
    expect(filterRows(SCORED, NO_FILTERS, NO_TAGS)).toHaveLength(125)
  })

  it("keeps any selected overall score (4 and 5: 50 of 125)", () => {
    const rows = filterRows(SCORED, only({ scores: new Set<Score>([4, 5]) }), NO_TAGS)
    expect(rows).toHaveLength(50)
    expect(new Set(rows.map((row) => row.overall))).toEqual(new Set([4, 5]))
  })

  it("never matches a score to a row without an overall", () => {
    const rows = [resultRow("p-jordan", "Jordan Bravo")]
    expect(filterRows(rows, only({ scores: new Set<Score>([1]) }), NO_TAGS)).toEqual([])
  })

  it("filters the table on the model's overall and export on the person's own score", () => {
    // test_browser_export_uses_human_scores_without_moving_displayed_rows.
    const rows = [
      resultRow("p-jordan", "Jordan Bravo", { overall: 4, human: 2 }),
      resultRow("p-casey", "Casey Delta", { overall: 3, human: 4 }),
      resultRow("p-morgan", "Morgan Echo", { overall: 4 }),
    ]
    const filters = only({ scores: new Set<Score>([4, 5]) })
    expect(ids(filterRows(rows, filters, NO_TAGS))).toEqual(["p-jordan", "p-morgan"])
    expect(ids(filterRows(rows, filters, NO_TAGS, exportScoreOf(new Map())))).toEqual(["p-casey", "p-morgan"])
  })

  it("keeps tagged people only, and within them any selected tag", () => {
    const rows = [
      resultRow("a", "Jordan Bravo"),
      resultRow("b", "Casey Delta"),
      resultRow("c", "Morgan Echo"),
    ]
    const tagged: Tagged = { tags: ["Backend", "Infra"], assignments: { a: ["Backend"], b: ["Infra"] } }
    expect(ids(filterRows(rows, only({ taggedOnly: true }), tagged))).toEqual(["a", "b"])
    expect(ids(filterRows(rows, only({ taggedOnly: true, tags: new Set(["Infra"]) }), tagged))).toEqual(["b"])
  })

  it("lets tagged only lapse when nobody is tagged, and drops removed tags from the filter", () => {
    const rows = [resultRow("a", "Jordan Bravo"), resultRow("b", "Casey Delta")]
    expect(filterRows(rows, only({ taggedOnly: true }), NO_TAGS)).toHaveLength(2)
    const tagged: Tagged = { tags: ["Backend"], assignments: { a: ["Backend"] } }
    const filters = only({ taggedOnly: true, tags: new Set(["Gone"]) })
    expect(liveFilters(filters, rows, tagged).tags).toEqual(new Set())
    expect(ids(filterRows(rows, filters, tagged))).toEqual(["a"])
  })

  it("matches anyone connected to a selected operator", () => {
    const rows = [
      resultRow("a", "Jordan Bravo", { operators: [DREW, EMERY] }),
      resultRow("b", "Casey Delta", { operators: [EMERY] }),
      resultRow("c", "Morgan Echo"),
    ]
    expect(ids(filterRows(rows, only({ operators: new Set(["op-1"]) }), NO_TAGS))).toEqual(["a"])
    expect(ids(filterRows(rows, only({ operators: new Set(["op-1", "op-2"]) }), NO_TAGS))).toEqual(["a", "b"])
  })

  it("keeps a person in each section they rank in; the count takes them once", () => {
    const first = { ...resultRow("a", "Jordan Bravo", { overall: 4 }), key: "0:a" }
    const second = { ...resultRow("a", "Jordan Bravo", { overall: 4 }), key: "1:a" }
    const filters = only({ scores: new Set<Score>([4]) })
    expect(keptRows([first, second], filters, NO_TAGS)).toEqual([first, second])
    expect(filterRows([first, second], filters, NO_TAGS)).toEqual([first])
  })

  it("keeps one row per person, the first", () => {
    const rows = [
      resultRow("a", "Jordan Bravo", { reason: "first" }),
      resultRow("a", "Jordan Bravo", { reason: "second" }),
    ]
    expect(filterRows(rows, NO_FILTERS, NO_TAGS).map((row) => row.reason)).toEqual(["first"])
  })
})

describe("toolbar values", () => {
  it("counts tagged people present in the rows", () => {
    const rows = [resultRow("a", "Jordan Bravo"), resultRow("b", "Casey Delta")]
    const tagged: Tagged = { tags: ["Backend"], assignments: { a: ["Backend"], elsewhere: ["Backend"] } }
    expect(taggedCount(rows, tagged)).toBe(1)
  })

  it("says N results, or N of M while filtered", () => {
    expect(countText(125, 125)).toBe("125 results")
    expect(countText(50, 125)).toBe("50 of 125 results")
    expect(countText(1, 1)).toBe("1 result")
  })

  it("lists each operator once, by name ignoring case", () => {
    const rows = [
      resultRow("a", "Jordan Bravo", { operators: [EMERY, DREW] }),
      resultRow("b", "Casey Delta", { operators: [DREW] }),
    ]
    expect(operatorOptions(rows)).toEqual([
      { id: "op-1", name: "Drew Kilo" },
      { id: "op-2", name: "emery Lima" },
    ])
  })
})
