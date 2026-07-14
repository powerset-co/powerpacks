# task_state

Internal JSON step log used by `search_network_pipeline.py` and compatibility
tools.

The standard `$search` flow does not ask an agent to design a task graph. The
pipeline initializes state, records primitive outputs, and writes audit events
as it executes its fixed sequence. Deep search uses its own plan binding and
epoch artifacts.

Direct CLI use is for diagnostics, tests, and old task-state migration:

```bash
uv run --project . python \
  packs/search/primitives/task_state/task_state.py --help
```

`init` creates a unique state file under `.powerpacks/runs/` unless `--out` is
provided. Every mutation appends a sibling `.events.jsonl` audit event.
`record-step` appends primitive output, and `append-lineage` retains explicit
feedback or run ancestry without silently rewriting earlier criteria.

`request-approval` and the `search_only` / `rerank` execution-mode records are
legacy compatibility surfaces. They are not the current user experience.
Standard search confirms the prepared preview once and continues with
`--execute-approved`; deep search performs one plan Review before sourcing.
