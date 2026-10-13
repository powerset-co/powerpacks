import { describe, expect, it } from "vitest"

import { decodePeople } from "@/lib/api/people"
import { savedEntry } from "@/testing/logbook-fixture"
import { PAYLOAD } from "@/testing/people-fixture"

import { LAST, lastBucket, nextTags, QUICK, sortRows, type FacetKey } from "./facets"
import { filterRows } from "./filter"
import { withLogbooks } from "./logbook"
import { DEFAULT_VIEW, type PeopleView } from "./view"

// Casey has a saved logbook.
const rows = withLogbooks(decodePeople(PAYLOAD), [savedEntry()])
const view: PeopleView = { ...DEFAULT_VIEW, tab: "confirm", sort: { key: "name", dir: 1 } }

describe("lastBucket", () => {
  it("splits at a year and two years", () => {
    expect(lastBucket(364)).toBe(LAST[0])
    expect(lastBucket(365)).toBe(LAST[1])
    expect(lastBucket(730)).toBe(LAST[1])
    expect(lastBucket(731)).toBe(LAST[2])
    expect(lastBucket(null)).toBe(LAST[3])
  })
})

describe("filterRows", () => {
  it("keeps the tab and sorts", () => {
    const { matching } = filterRows(rows, view)
    expect(matching.map((row) => row.name)).toEqual(["Casey Delta", "Jordan Bravo", "Riley Echo"])
  })

  it("keeps the rows that match every held facet", () => {
    const filters = new Map<FacetKey, Set<string>>([
      ["relationship_kind", new Set(["family"])],
      ["last", new Set([LAST[0]])],
    ])
    const { matching } = filterRows(rows, { ...view, filters })
    expect(matching.map((row) => row.name)).toEqual(["Jordan Bravo"])
  })

  it("counts quick filters within the tab, before text and facets", () => {
    const filters = new Map<FacetKey, Set<string>>([["worth", new Set(["yes"])]])
    const { quickCounts } = filterRows(rows, { ...view, filters, text: "jordan" })
    const byName = Object.fromEntries(QUICK.map((quick, position) => [quick.name, quickCounts[position]]))
    expect(byName).toMatchObject({
      Family: 2,
      Recruiters: 1,
      "Close friends": 1,
      "Last contact > 2 years": 1,
    })
  })

  it("filters to the people with a saved logbook", () => {
    const filters = new Map<FacetKey, Set<string>>([["logbook", new Set(["Has logbook"])]])
    const { matching } = filterRows(rows, { ...view, filters })
    expect(matching.map((row) => row.name)).toEqual(["Casey Delta"])
  })

  it("counts no one under Has logbook before the saved logbooks are read", () => {
    const { quickCounts } = filterRows(decodePeople(PAYLOAD), view)
    expect(quickCounts[QUICK.findIndex((quick) => quick.name === "Has logbook")]).toBe(0)
  })

  it("matches text on name, title, company and location", () => {
    expect(filterRows(rows, { ...view, text: " ACME " }).matching.map((row) => row.name)).toEqual([
      "Jordan Bravo",
    ])
    expect(filterRows(rows, { ...view, tab: "yes", text: "springfield" }).matching).toHaveLength(1)
  })
})

describe("sortRows and nextTags", () => {
  it("sorts by interactions, most first", () => {
    expect(sortRows(rows, { key: "messages", dir: 1 }).map((row) => row.interactions)).toEqual([
      90, 40, 12, 3,
    ])
  })

  it("swaps share and private", () => {
    const riley = rows.find((row) => row.name === "Riley Echo")
    expect(riley && nextTags(riley, "share")).toEqual(["share"])
    expect(riley && nextTags(riley, "private")).toEqual(["private"])
  })
})
