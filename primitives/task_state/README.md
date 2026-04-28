# task_state

Create and update a JSON task run while `$search-network` executes.

Use this primitive to keep the plan, step outputs, and final assessment in one
file. It is intentionally local and simple.

Examples:

```bash
python powerpacks/primitives/task_state/task_state.py init \
  --query "software engineers in sf" \
  --out /tmp/search-network-run.json
```

```bash
python powerpacks/primitives/task_state/task_state.py record-step \
  --state /tmp/search-network-run.json \
  --step-id direct_count \
  --status completed \
  --output-json '{"total_count":2543}'
```

The state file should validate against `schemas/task-run.schema.json`.
