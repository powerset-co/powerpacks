# Powerpacks Data Contracts

These files are the checked-in source of truth for Powerpacks primitives.

Agents and primitives should read these contracts instead of guessing live table
or attribute names. Live schema inspection is allowed only through the explicit
`primitives/contracts` diagnostic primitive, and generated dumps should be kept
as artifacts unless a human decides to update these checked-in contracts.

## Layout

- `postgres/*.table.json` describes required Postgres/Supabase tables and
  columns used by hydration and support primitives.
- `turbopuffer/*.namespace.json` describes TurboPuffer namespaces, document
  grain, searchable attributes, and supported filter operators.
- `profiles/hydrated-profile.schema.json` describes the normalized hydrated
  candidate profile passed between hydration, LLM filtering, persistence, and
  review UI primitives.

## Diagnostics

```bash
uv run --project . python packs/search/primitives/contracts/contracts.py list
uv run --project . python packs/search/primitives/contracts/contracts.py show postgres/persons.table.json
uv run --env-file .env --project . python packs/search/primitives/contracts/contracts.py check-postgres
uv run --env-file .env --project . python packs/search/primitives/contracts/contracts.py dump-postgres \
  --out .powerpacks/schema-dumps/postgres-live.json
```
