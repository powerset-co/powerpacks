# persist_search_results

Write reusable search-result artifacts from a completed task run.

Use this after `merge_candidate_frontier` or `hydrate_people` so the user can
build on a search later.

Artifacts:

- CSV for spreadsheets and lightweight review
- JSONL for agentic refinement and downstream tools
- manifest JSON with paths, counts, task ID, and original query
- task-state `artifacts` block pointing to the files

Commands:

```bash
python powerpacks/primitives/persist_search_results/results_io.py export \
  --state .powerpacks/runs/search-network-<uuid>.json
```

```bash
python powerpacks/primitives/persist_search_results/results_io.py view \
  --state .powerpacks/runs/search-network-<uuid>.json \
  --limit 25
```

Rules:

- Persist results before the final response when a filesystem is available.
- Prefer JSONL as the refinement substrate.
- CSV is for user review and interoperability.
- Do not hide unhydrated frontier IDs; include them with `hydrated=false`.
