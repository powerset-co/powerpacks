# hydrate_people

Hydrate the frontier's person IDs into richer profile objects suitable
for final answer formatting and follow-up filtering.

By default, verbose hydrated profiles are written to local JSONL handoff files
and the task state stores only file paths/counts/IDs. Downstream primitives read
those paths directly so the agent does not shuttle large profile blobs through
chat or command arguments.

A local search reads the local DuckDB index (`--local-db`). A set search asks the
Powerset API: `POST /v2/hydrate` for the profiles and
`POST /v2/sets/{id}/person-attribution` for who in the set knows each person.
Neither reads Postgres or TurboPuffer.

Hydrate the full frontier before `llm_filter_candidates`:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/hydrate_people/hydrate_people.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

Dry-run without reading any profiles:

```bash
uv run --project . python \
  packs/search/primitives/hydrate_people/hydrate_people.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --dry-run
```

Outputs in task state:

- `profiles_path` — full hydrated profile JSONL, gzip-compressed by default as
  `profiles.jsonl.gz`, for downstream primitives and rerank
- `llm_profiles_path` — compact LLM-filter handoff JSONL for current-role queries
- `profiles_compressed`, `profile_ids`, `requested`, `hydrated`

Pass `--no-compress-profiles` only when you need a plaintext raw
`profiles.jsonl`. Pass `--dump-profiles` only when you want an additional debug
`profiles.json`.
