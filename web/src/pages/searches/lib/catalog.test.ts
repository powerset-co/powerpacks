import { describe, expect, it } from "vitest"

import { CARDS } from "@/testing/searches-fixture"

import { statusText } from "./copy"
import {
  groupByRecency,
  initialFilter,
  matches,
  recencyTitle,
  statusesOf,
  versionsOf,
  type CatalogFilter,
} from "./catalog"

// A Saturday, so Sunday the 20th starts "This week".
const NOW = new Date(2026, 8, 26, 12)

describe("catalog filter", () => {
  it("lists stamped versions newest first, never an unversioned one, and preselects the newest", () => {
    expect(versionsOf(CARDS)).toEqual(["2026-09-26", "2026-09-01"])
    expect(initialFilter(CARDS)).toEqual({ text: "", version: "2026-09-26", company: "", status: "" })
  })

  it("matches text over title, company and run id, and the selects by value", () => {
    const all: CatalogFilter = { text: "", version: null, company: "", status: "" }
    const shown = (filter: CatalogFilter) =>
      CARDS.filter((card) => matches(card, filter)).map((card) => card.run_id)
    expect(shown(all)).toHaveLength(4)
    expect(shown({ ...all, text: "sample" })).toEqual(["casey-role", "riley-role"])
    expect(shown({ ...all, text: "MORGAN-ROLE" })).toEqual(["morgan-role"])
    expect(shown({ ...all, status: "Running" })).toEqual(["casey-role"])
    expect(shown({ ...all, version: "2026-09-01" })).toEqual(["morgan-role"])
  })

  it("says statuses in plain words, one option per wording", () => {
    expect(statusText("awaiting_diagnosis")).toBe("Search complete")
    expect(statusText("completed")).toBe("Search complete")
    expect(statusText("needs_REVIEW")).toBe("Needs review")
    expect(statusesOf(CARDS)).toEqual(["Running", "Search complete"])
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
