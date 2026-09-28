import { describe, expect, it } from "vitest"

import { CARDS } from "@/testing/searches-fixture"

import { groupByRecency, matches, parseCatalogFilter, recencyTitle, type CatalogFilter } from "./catalog"
import { statusText } from "./copy"

// A Saturday, so Sunday the 20th starts "This week".
const NOW = new Date(2026, 8, 26, 12)

describe("catalog filter", () => {
  it("matches text over title, company and run id", () => {
    const shown = (filter: CatalogFilter) =>
      CARDS.filter((card) => matches(card, filter)).map((card) => card.run_id)
    expect(shown({ text: "" })).toHaveLength(4)
    expect(shown({ text: "sample" })).toEqual(["casey-role", "riley-role"])
    expect(shown({ text: "MORGAN-ROLE" })).toEqual(["morgan-role"])
  })

  it("says statuses in plain words", () => {
    expect(statusText("awaiting_diagnosis")).toBe("Search complete")
    expect(statusText("completed")).toBe("Search complete")
    expect(statusText("needs_REVIEW")).toBe("Needs review")
  })
})

describe("saved catalog filter", () => {
  it("reads back the search text and rejects any other shape", () => {
    expect(parseCatalogFilter({ text: "design" })).toEqual({ text: "design" })
    expect(parseCatalogFilter({ text: 3 })).toBeNull()
    expect(parseCatalogFilter([])).toBeNull()
  })
})

describe("recency groups", () => {
  it("names today, yesterday, this week, then the month, with the year when it differs", () => {
    expect(recencyTitle(new Date(2026, 8, 26, 1).toISOString(), NOW)).toBe("Today")
    expect(recencyTitle(new Date(2026, 8, 25, 23).toISOString(), NOW)).toBe("Yesterday")
    expect(recencyTitle(new Date(2026, 8, 20, 9).toISOString(), NOW)).toBe("This week")
    expect(recencyTitle(new Date(2026, 8, 19, 9).toISOString(), NOW)).toBe("September")
    expect(recencyTitle(new Date(2025, 11, 2).toISOString(), NOW)).toBe("December 2025")
    expect(recencyTitle("", NOW)).toBe("Earlier")
  })

  it("keeps the catalog's order inside and across groups", () => {
    expect(groupByRecency(CARDS, NOW).map((group) => [group.title, group.cards.length])).toEqual([
      ["Today", 1],
      ["Yesterday", 1],
      ["September", 1],
      ["December 2025", 1],
    ])
  })
})
