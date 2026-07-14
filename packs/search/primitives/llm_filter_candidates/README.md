# llm_filter_candidates

Filter hydrated search candidates with the same fast pre-screening prompt used
by the Aleph search pipeline.

This is not expensive scoring or reranking. It is a conservative "kick out
clearly bad candidates" pass:

- run only after `hydrate_people`
- hydrate the full frontier first, not just the visible shortlist
- score candidates from 0.0 to 1.0
- keep candidates with score >= 0.3 by default
- when uncertain, include the candidate
- record filtered people with reason and score
- run small batches concurrently

Usage:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --write-state
```

Dry-run without calling OpenAI:

```bash
uv run --project . python \
  packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --dry-run
```

Inputs:

- task state with a retrieved and hydrated candidate frontier
- `hydrate_people.output.profiles_path` / `llm_profiles_path` covering the candidate frontier
- `OPENAI_API_KEY`

Profile handoff:

- `--profile-scope auto` is the default.
- Auto uses compact `llm_profiles_path` only when role filters are current-role
  scoped (`is_current: true`).
- Auto uses full `profiles_path` for all-time/past-role queries.
- Override with `--profile-scope current` or `--profile-scope all`.

Batching and concurrency:

- `--batch-size` defaults to `2`.
- `--concurrency` defaults to `$POWERPACKS_LLM_FILTER_CONCURRENCY`, then
  `$SEARCH_V2_LLM_FILTER_MAX_CONCURRENT`, then `1000`.
- Progress is written to stderr as batches complete.

Outputs:

- `llm_filter_candidates` step in task state with passed/filtered IDs and counts
- no artifacts by default
- pass `--dump-debug` to write local score/filter/prompt JSONL files
