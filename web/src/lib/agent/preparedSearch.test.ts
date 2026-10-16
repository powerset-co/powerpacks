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

it("reads the preview's traits and keeps each filter's key beside its label", () => {
  const search = preparedSearch(
    prepared({
      query: "VP of finance in New York",
      status: "preview_ready",
      preview: {
        filters: { metro_areas: ["New York Metropolitan Area"], is_current_role: true },
        traits: [
          { meaning: "role", temporal: "current", value: "VP of Finance" },
          { meaning: "experience", temporal: "past", value: "investment banking" },
        ],
      },
    }),
  )
  expect(search?.filters).toEqual([
    { key: "metro_areas", label: "Metro areas", value: "New York Metropolitan Area" },
    { key: "is_current_role", label: "Is current role", value: "true" },
  ])
  expect(search?.traits).toEqual([
    { value: "VP of Finance", temporal: "current" },
    { value: "investment banking", temporal: "past" },
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
})
