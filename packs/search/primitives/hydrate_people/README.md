# hydrate_people

Hydrate person IDs returned by TurboPuffer into richer profile objects suitable
for final answer formatting and follow-up filtering.

Powerpacks hydrates directly from the checked-in Postgres/Supabase contract.
It reads canonical `persons.hydrated_context` and normalizes that into the
Powerpacks profile shape. It does not import `aleph-mvp` and does not use
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

Credentials can come from `DATABASE_URL`, `SUPABASE_DATABASE_URL`,
`SUPABASE_DB_URL`, or `POSTGRES_*` environment variables. The script uses
`psycopg2`; if it is not installed and `uv` is available, it re-runs itself with
`uv run --with psycopg2-binary`.
