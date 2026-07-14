# persist_search_results

Write reusable search-result artifacts from a completed task run.

The standard pipeline calls this after hydration and its configured filter and
rerank stages. Direct use is for diagnostics or compatible task states.

Artifacts:

- CSV for spreadsheets and lightweight review
- JSONL for agentic refinement and downstream tools
- manifest JSON with paths, counts, task ID, and original query
- task-state `artifacts` block pointing to the files

Commands:

```bash
uv run --project . python \
  packs/search/primitives/persist_search_results/results_io.py export \
  --state .powerpacks/runs/search-network-<uuid>.json
```

```bash
uv run --project . python \
  packs/search/primitives/persist_search_results/results_io.py view \
  --state .powerpacks/runs/search-network-<uuid>.json \
  --limit 25
```

Rules:

- Persist results before the final response when a filesystem is available.
- Prefer JSONL as the refinement substrate.
- CSV is for user review and interoperability.
- Do not hide unhydrated frontier IDs; include them with `hydrated=false`.
