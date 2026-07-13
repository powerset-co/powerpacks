# Task Harness

> **Legacy V1 design reference.** This is not the current `$search` harness
> contract. See the canonical [`$search` architecture](search-architecture.md)
> and executable [`$search` skill](../skills/search/SKILL.md).

Powerpacks uses tasks as the plan/execute ledger for `/search-network`.

## Contract

- Skills define reasoning workflow.
- Tasks define step order, input/output expectations, and state.
- Primitives do executable work or deterministic state updates.

## Search Network Task

The task template lives at:

- `powerpacks/tasks/search-network.task.json`

Each run should create state that validates against:

- `powerpacks/packs/search/schemas/task-run.schema.json`

By default, initialize a unique state file under:

- `.powerpacks/runs/search-network-<uuid>-<query-slug>.json`

Every state write must also append an audit event beside it:

- `.powerpacks/runs/search-network-<uuid>-<query-slug>.json.events.jsonl`

Return this path in the final answer so the user can inspect or resume the run.

The run state should include:

- original query
- active constraints
- every step output
- strategy decisions
- counts and slice yields
- frontier assessment
- result artifact paths
- final summary

## Execution Rule

Run one step, record its output, then decide the next step from current state.
Do not assume slicing is mandatory.

## Approval Gate

The default UX should be plan-and-confirm:

- generate a concrete plan
- record it in task state
- set task status to `awaiting_approval`
- wait for one of: search only, rerank, or requested changes

Use `task_state.py request-approval` whenever the next step will spend real
retrieval/hydration/LLM budget, create a large frontier, or materially change
the search meaning.

Approval actions:

- `approve --execution-mode search_only`: user accepts retrieval, hydration,
  and normal persistence; task status returns to `running`
- `approve --execution-mode rerank`: user accepts retrieval, hydration, normal
  persistence, and sharded agentic review after hydration
- `request-changes --note "<instruction>"`: user changes the plan; task status
  becomes `paused` until the agent updates the plan

The constraints still apply: no summary search, no company-signal search, and
no LLM candidate enrichment. Agentic reranking is allowed only when approval
records `execution_mode = "rerank"`.

For slice searches, every slice must record:

- knobs used
- count result
- candidate limit
- hydration limit
- returned candidate IDs
- slice-local summary

## Result Artifacts

After retrieval, hydrate the full frontier, optionally run the conservative LLM
filter, then run `persist_search_results` to write:

- CSV for human review
- JSONL for refinement and automation
- manifest JSON for artifact discovery

The task state's `artifacts` block should point to those files.

For interactive review, host-specific tooling should read the same task state
and write a sibling review JSONL file. Review events become the structured
input to a later `refine_search_results` child task.

## Sharded Agent Review

For Codex and Claude Code harnesses, use `agentic_candidate_review` after
hydration when approval records `execution_mode = "rerank"` or when the user
asks to rerank/review an existing completed run.

The primitive has two deterministic phases:

- `prepare`: writes immutable `review_shards/*.json` files plus
  `review_manifest.json`, then records the manifest and shard paths in the task
  JSON under `artifacts.agentic_candidate_review`.
- `reduce`: validates `review_outputs/*.jsonl`, merges all shard reviews, and
  writes one sorted `ranked_candidates.jsonl` plus one sorted
  `ranked_candidates.csv`, then records final ranked artifact paths in the same
  task JSON.

Do not let reviewer agents append to a shared CSV. Shards are for parallelism;
the reducer owns the final user-facing sorted files.

## V1 Constraints

- no summary search
- no company-signal search
- no LLM candidate enrichment
- no automatic expensive scoring or reranking during normal search execution
- LLM filtering is allowed only as a conservative pre-screen after hydration
- agentic candidate review is an explicit harness step and must write auditable
  artifacts before presenting ranked output

Query expansion can still use the existing extraction stack. That is the
decomposition stage, not candidate enrichment.
