# task_state

Create and update a JSON task run while `/search-network` executes.

Use this primitive to keep the plan, step outputs, and final assessment in one
file. It is intentionally local and simple.

Examples:

```bash
python powerpacks/primitives/task_state/task_state.py init \
  --query "software engineers in sf"
```

By default this writes a unique run file under
`.powerpacks/runs/search-network-<uuid>-<query-slug>.json`. Use `--out` only
when an explicit path is required.

Every write also appends an audit event to
`.powerpacks/runs/<run-file>.events.jsonl`.

```bash
python powerpacks/primitives/task_state/task_state.py record-step \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --step-id direct_count \
  --status completed \
  --output-json '{"total_count":2543}'
```

The state file should validate against `schemas/task-run.schema.json`.

Approval records both execution approval and whether post-hydration agentic
review should run:

```bash
python powerpacks/primitives/task_state/task_state.py approve \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --execution-mode search_only
```

```bash
python powerpacks/primitives/task_state/task_state.py approve \
  --state .powerpacks/runs/search-network-<uuid>-software-engineers-in-sf.json \
  --execution-mode rerank
```
