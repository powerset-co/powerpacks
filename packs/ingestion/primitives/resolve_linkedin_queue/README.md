# resolve_linkedin_queue

Resolve queued LinkedIn URLs using the shared prompt in `packs/ingestion/prompts/linkedin_resolution.md`.

Modes:

- `harness`: no spend; writes `instructions.md` and `harness_prompts.jsonl` for Codex/Claude/manual resolution.
- `parallel`: spend-bearing; blocks until `approve`, then submits to Parallel.ai.

```bash
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --provider harness \
  --input .powerpacks/network-import/discover/twitter/<handle>/linkedin_resolution_queue.csv
```

Parallel:

```bash
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --provider parallel \
  --input .powerpacks/network-import/discover/twitter/<handle>/linkedin_resolution_queue.csv
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py approve
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py continue --wait
```

Outputs `linkedin_resolutions.csv` with `handle,status,linkedin_url,confidence,matched_name,matched_headline,evidence,reasoning`.
