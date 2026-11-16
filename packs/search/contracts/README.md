# Powerpacks Data Contracts

These files are the checked-in source of truth for Powerpacks primitives.

Agents and primitives should read these contracts instead of guessing table or
attribute names. The local index mirrors these shapes; an install never reads
Postgres or TurboPuffer itself (a set goes through the Powerset API).

## Layout

- `postgres/*.table.json` describes required Postgres/Supabase tables and
  columns used by hydration and support primitives.
- `turbopuffer/*.namespace.json` describes TurboPuffer namespaces, document
  grain, searchable attributes, and supported filter operators.
- `profiles/hydrated-profile.schema.json` describes the normalized hydrated
  candidate profile passed between hydration, LLM filtering, persistence, and
  review UI primitives.
