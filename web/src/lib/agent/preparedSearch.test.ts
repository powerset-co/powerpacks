import { expect, it } from "vitest"

import type { Entry } from "@/types/agent"

import { preparedSearch, ranSearch, refineStep, stepCount, stepVerb, wentBack } from "./preparedSearch"

// The agent may run the primitive inside a script: the card comes from what it printed.
function prepared(printed: Record<string, unknown>): Entry {
  return {
    kind: "command",
    id: "c1",
    command: "/bin/zsh -lc \"uv run python - <<'PY'\nimport subprocess\nsubprocess.run(prepare)\nPY\"",
    status: "done",
    exitCode: 0,
    output: `warning: uv is resolving\n${JSON.stringify({ primitive: "search_network_pipeline", ...printed }, null, 2)}\n`,
    durationMs: 3900,
  }
}

it("knows the search's run by what it printed, and a prepare or any other command as not it", () => {
  expect(ranSearch(prepared({ status: "completed", summary: { rows: 207 } }))).toBe(true)
  expect(
    ranSearch(prepared({ query: "VP of finance", status: "preview_ready", preview: { filters: {} } })),
  ).toBe(false)
  const logged: Entry = {
    kind: "command",
    id: "c2",
    command: "uv run python search_feedback.py log --kind query_edit",
    status: "done",
    exitCode: 0,
    output: '{"status": "logged", "kind": "query_edit"}',
    durationMs: 900,
  }
  expect(ranSearch(logged)).toBe(false)
  expect(preparedSearch(prepared({ status: "completed", summary: { rows: 207 } }))).toBeNull()
})

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
    { value: "Series B", temporal: "all" },
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

it("names the prepare's folder under .powerpacks/search, where the card saves its edits", () => {
  const search = (payload_json: string) =>
    preparedSearch(prepared({ query: "VP", status: "preview_ready", preview: { filters: {} }, payload_json }))
      ?.run
  expect(search("/Users/casey/powerpacks/.powerpacks/search/ab12-vp/expand_search_request.json")).toBe(
    "ab12-vp",
  )
  expect(search("/tmp/engine/run/expand_search_request.json")).toBeNull()
})

const RERANK =
  "uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py rerank --run-dir '.powerpacks/search/ml-infra'"

function reranked(command: string, step: Record<string, unknown>): Entry {
  return {
    kind: "command",
    id: "r1",
    command,
    status: "done",
    exitCode: 0,
    output: `scoring 120 people\n${JSON.stringify({ primitive: "search_network_pipeline", status: "completed", step }, null, 2)}\n`,
    durationMs: 8000,
  }
}

function step(id: string, parent: string | null, criteria: string | null, query_type: string | null) {
  return { run_id: "ml-infra", id, parent, criteria, query_type, shown: 64, scored: 120, cost_usd: 0.01 }
}

it("reads a refinement's verb, what changed and its count from what rerank printed", () => {
  const ranked = refineStep(
    reranked(
      `${RERANK} --criteria 'built training infra at scale'`,
      step("s2", "s1", "built training infra at scale", "rerank"),
    ),
  )
  expect(ranked && [stepVerb(ranked), ranked.criteria, stepCount(ranked), ranked.run_id]).toEqual([
    "Re-ranked",
    "built training infra at scale",
    "64 people",
    "ml-infra",
  ])
  const both = refineStep(
    reranked(`${RERANK} --criteria 'prefer ML'`, step("s2", "s1", "prefer ML", "filter_and_rank")),
  )
  expect(both && stepVerb(both)).toBe("Re-ranked")
  const narrowed = refineStep(
    reranked(`${RERANK} --criteria 'only startups'`, step("s3", "s2", "still at a startup", "filter")),
  )
  expect(narrowed && [stepVerb(narrowed), stepCount(narrowed)]).toEqual(["Narrowed", "64 of 120 people"])
  const searched = refineStep(reranked(`${RERANK} --criteria 'x'`, step("s1", null, null, null)))
  expect(searched && stepVerb(searched)).toBe("Searched")
  // A refinement is not the search's own result, and no card is made of it.
  const entry = reranked(`${RERANK} --criteria 'x'`, step("s2", "s1", "x", "rerank"))
  expect(ranSearch(entry)).toBe(false)
  expect(preparedSearch(entry)).toBeNull()
})

it("knows a go-back by its command: its printed step is not a refinement", () => {
  const back = reranked(`${RERANK} --use 's1'`, step("s1", null, null, null))
  expect(wentBack(back)).toEqual({ runId: "ml-infra", stepId: "s1" })
  expect(refineStep(back)).toBeNull()
  // The agent's own run of it, inside the shell wrapper's quoting.
  const wrapped = `/bin/zsh -lc '${RERANK.replaceAll("'", `'"'"'`)} --use '"'"'s2'"'"''`
  expect(wentBack(reranked(wrapped, step("s2", "s1", "x", "rerank")))).toEqual({
    runId: "ml-infra",
    stepId: "s2",
  })
  expect(wentBack(reranked(`${RERANK} --criteria 'x'`, step("s2", "s1", "x", "rerank")))).toBeNull()
  expect(wentBack(prepared({ status: "completed", summary: { rows: 207 } }))).toBeNull()
})
