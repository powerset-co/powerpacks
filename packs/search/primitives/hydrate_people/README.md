# hydrate_people

Hydrate person IDs returned by TurboPuffer into richer profile objects suitable
for final answer formatting and follow-up filtering.

By default, verbose hydrated profiles are written to local JSONL handoff files
and the task state stores only file paths/counts/IDs. Downstream primitives read
those paths directly so the agent does not shuttle large profile blobs through
chat or command arguments.

Powerpacks hydrates directly from the checked-in Postgres/Supabase contract.
It reads canonical `persons.hydrated_context` and normalizes that into the
Powerpacks profile shape. It does not import any external app repo and does not use
Supabase MCP.

Hydrate the full frontier before `llm_filter_candidates`:

```bash
python powerpacks/primitives/hydrate_people/hydrate_people.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

Dry-run without connecting to Postgres:

```bash
python powerpacks/primitives/hydrate_people/hydrate_people.py \
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

Credentials can come from `DATABASE_URL`, `SUPABASE_DATABASE_URL`,
`SUPABASE_DB_URL`, or `POSTGRES_*` environment variables. The script uses
`psycopg2`; if it is not installed and `uv` is available, it re-runs itself with
`uv run --with psycopg2-binary`.
