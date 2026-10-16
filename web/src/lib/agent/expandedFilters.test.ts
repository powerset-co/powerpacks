import { expect, it } from "vitest"

import type { FilterValue } from "./preparedSearch"

import { fromExpanded, toExpanded } from "./expandedFilters"

it("maps names to entity items and back, money in millions on the modal side", () => {
  const values = new Map<string, FilterValue | null>([
    ["company_names", ["Snap"]],
    ["metro_areas", ["Los Angeles Metropolitan Area"]],
    ["funding_amount_min", 100_000_000],
    ["bm25_queries", ["swe"]],
    ["years_experience_min", 3],
  ])
  const filters = toExpanded(values, { seniority_bands: {} })
  expect(filters.company_ids).toEqual([{ id: "Snap", display_value: "Snap", type: "company_ids" }])
  expect(filters.metro_areas[0]?.type).toBe("metro_area")
  expect(filters.funding_amount_min).toBe(100)
  expect(filters.role_bm25_queries).toEqual(["swe"])

  filters.funding_amount_min = 250
  filters.company_ids = []
  filters.cities = [{ id: "Brooklyn", display_value: "Brooklyn", type: "city" }]
  const back = fromExpanded(filters, values)
  expect(back.get("funding_amount_min")).toBe(250_000_000)
  expect(back.get("company_names")).toBeNull()
  expect(back.get("cities")).toEqual(["Brooklyn"])
  expect(back.get("years_experience_min")).toBe(3)
})
