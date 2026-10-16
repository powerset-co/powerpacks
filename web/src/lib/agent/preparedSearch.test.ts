import { expect, it } from "vitest"

import type { Entry } from "@/types/agent"

import { preparedSearch } from "./preparedSearch"

function prepared(printed: Record<string, unknown>): Entry {
  return {
    kind: "command",
    id: "c1",
    command: "uv run python search_network_pipeline.py prepare --backend local --query 'VP of finance'",
    status: "done",
    exitCode: 0,
    output: `warning: uv is resolving\n${JSON.stringify(printed, null, 2)}\n`,
    durationMs: 3900,
  }
}

it("keeps each filter's value as the payload holds it, the role intent after the filters", () => {
  const search = preparedSearch(
    prepared({
      query: "VP of finance in New York",
      status: "preview_ready",
      preview: {
        filters: {
          metro_areas: ["New York Metropolitan Area"],
          years_experience_min: 5,
          is_current_role: true,
          company_names: [],
          age_max: null,
        },
        role_title_intent: {
          semantic_query: "Finance leaders",
          bm25_queries: ["vp finance", "head of finance"],
        },
        traits: [
          { meaning: "role", temporal: "current", value: "VP of Finance" },
          { meaning: "experience", temporal: "past", value: "investment banking" },
          { temporal: "all", value: "Series B" },
        ],
      },
    }),
  )
  expect(search?.filters).toEqual([
    { key: "metro_areas", value: ["New York Metropolitan Area"] },
    { key: "years_experience_min", value: 5 },
    { key: "is_current_role", value: true },
    { key: "semantic_query", value: "Finance leaders" },
    { key: "bm25_queries", value: ["vp finance", "head of finance"] },
  ])
  expect(search?.traits).toEqual([
    { value: "VP of Finance", temporal: "current", meaning: "role" },
    { value: "investment banking", temporal: "past", meaning: "experience" },
    { value: "Series B", temporal: "all", meaning: "role" },
  ])
})

it("has no traits when the preview carries none, as replies from older builds", () => {
  const search = preparedSearch(
    prepared({
      query: "VP of finance in New York",
      status: "preview_ready",
      preview: { filters: { metro_areas: ["New York Metropolitan Area"] } },
    }),
  )
  expect(search?.traits).toEqual([])
  expect(search?.filters).toEqual([{ key: "metro_areas", value: ["New York Metropolitan Area"] }])
})
