// A quick run's refinements (results.json `steps`): the search is s1, each rerank adds a step on
// the one showing. Going back runs `rerank --use` on the chat with no model turn; the page never
// shows a step's id.

import { runCommand, send } from "@/lib/agent/store"
import type { SearchResult, SearchStep } from "@/types/searches"

const PIPELINE =
  "uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py"

/** The steps from the search to the one showing, walked back by parent; empty for a run without steps. */
export function stepTrail(search: SearchResult): SearchStep[] {
  const byId = new Map(search.steps.map((step) => [step.id, step]))
  const trail: SearchStep[] = []
  let step = search.current_step === null ? undefined : byId.get(search.current_step)
  while (step) {
    trail.unshift(step)
    step = step.parent === null ? undefined : byId.get(step.parent)
  }
  return trail
}

/** A filter step narrows: "only" its criteria. */
export function isFilter(step: SearchStep): boolean {
  return step.query_type === "filter"
}

/** What a step is called on screen: the search's title for s1, "only …" for a filter, else the criteria. */
export function stepLabel(title: string, step: SearchStep): string {
  if (step.criteria === null) return title
  return isFilter(step) ? `only ${step.criteria}` : step.criteria
}

// Shell-style: an apostrophe becomes '"'"'.
function quoted(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

export function goBackCommand(runId: string, stepId: string): string {
  return `${PIPELINE} rerank --run-dir ${quoted(`.powerpacks/search/${runId}`)} --use ${quoted(stepId)}`
}

/** Shows `step` again: the pipeline on the chat straight away, or, with no chat yet, a message asking for it. */
export function goBack(search: SearchResult, step: SearchStep): void {
  void runCommand(goBackCommand(search.run_id, step.id)).then((started) =>
    started ? undefined : send(`Go back to ${stepLabel(search.title, step)}`),
  )
}
