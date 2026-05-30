# task_state

Create and update a JSON task run while `/search-network` executes.

Use this primitive to keep the plan, step outputs, and final assessment in one
file. It is intentionally local and simple.

Examples:

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py init \
  --query "software engineers in sf"
```

By default this writes a unique run file under
`.powerpacks/runs/search-network-<uuid>-<query-slug>.json`. Use `--out` only
when an explicit path is required.

Every write also appends an audit event to
`.powerpacks/runs/<run-file>.events.jsonl`.

Use `request-approval --plan-json` to record the intended checklist before
retrieval:

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py request-approval \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --reason "Search requires external retrieval." \
  --proposed-next-step "Resolve education, prefilter, count, retrieve, hydrate, export." \
  --plan-json '{"planned_steps":["resolve_education","apply_prefilters","count_candidates","execute_role_search","hydrate_people","persist_search_results"]}'
```

This writes `planned_steps[]` as a mutable checklist. `steps[]` stays the
append-only execution log. When `record-step` is called for a matching planned
step, the planned step is marked completed/failed/skipped with timestamps.
`--plan-json` should normally be an object with `planned_steps`, but a bare
array of planned step IDs is also accepted and normalized as `planned_steps`.

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py record-step \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --step-id count_candidates \
  --status completed \
  --output-json '{"total_count":2543}'
```

The state file should validate against `schemas/task-run.schema.json`.

Use `append-lineage` to preserve feedback-driven refinement over multiple
rounds instead of overwriting criteria invisibly. Supported `--kind` values are
`candidate_feedback`, `criteria_mutation`, `search_plan_revision`,
`run_lineage`, `exemplar_set`, and `fanout_thread`:

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py append-lineage \
  --state .powerpacks/runs/search-network-<uuid>-cto-search.json \
  --kind candidate_feedback \
  --payload-json '{"person_id":"abc","label":"false_positive","reason":"not technical","applied_to_next_search":true}'
```

Each lineage append updates the matching top-level array in the state file and
writes an `append_lineage` row to the `.events.jsonl` audit log. The helper
generates stable ids such as `feedback_id` / `mutation_id` when omitted and
auto-increments `search_plan_revision.revision` when omitted.

Approval records both execution approval and whether post-hydration agentic
review should run:

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py approve \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --execution-mode search_only
```

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py approve \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --execution-mode rerank
```
