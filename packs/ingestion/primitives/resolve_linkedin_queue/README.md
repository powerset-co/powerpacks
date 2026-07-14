# resolve_linkedin_queue

Resolve queued LinkedIn URLs using the shared prompt in `packs/ingestion/prompts/linkedin_resolution.md`.

Modes:

- `harness`: no spend; writes `instructions.md` and `harness_prompts.jsonl` for Codex/Claude/manual resolution.
- `parallel`: spend-bearing; a first `run` without `--approve-spend` returns
  `blocked_approval`, then an approved rerun submits to Parallel.ai.

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

# After the user approves the displayed estimate:
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --provider parallel \
  --approve-spend \
  --input .powerpacks/network-import/discover/twitter/<handle>/linkedin_resolution_queue.csv
```

The current CLI exposes `run` and `status`; it has no separate `approve` or
`continue` subcommand. Completed output rows are reused. An interrupted provider
task may be resubmitted because the ledger does not expose a continuation command.

Outputs `linkedin_resolutions.csv` with `handle,status,linkedin_url,confidence,matched_name,matched_headline,evidence,reasoning`.
