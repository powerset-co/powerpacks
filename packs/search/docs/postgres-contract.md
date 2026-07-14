# Postgres hydration contract

Powerpacks does not introspect database columns during normal search. The
checked-in JSON under [`contracts/postgres/`](../contracts/postgres/) is the
source of truth for table names, required columns, types, and consumers. This
page explains the human-facing boundary rather than duplicating those lists.

## Credentials

Provide one URL:

- `DATABASE_URL`
- `SUPABASE_DATABASE_URL`
- `SUPABASE_DB_URL`

Or provide `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and
`POSTGRES_PASSWORD`. `POSTGRES_SSLMODE` defaults to `require`.

## Search responsibilities

| Tables | Purpose |
| --- | --- |
| `sets`, `set_members`, `users` | Resolve a Powerset set to the operator IDs used to scope TurboPuffer retrieval. |
| `persons` | Hydrate retrieved base person IDs into canonical profile evidence. |
| `person_source_summary` | Optionally add interaction totals; missing rows or a missing optional table must not fail hydration. |
| `companies` | Backfill company details and support company lookup or resolution. |

TurboPuffer results may be position-grained. Hydration must normalize them to a
base person ID before reading `persons`; a position ID must not be used as if it
were a person ID. `persons.hydrated_context` is the canonical nested profile
passed to filtering, judging, persistence, and review.

Use the checked-in contracts rather than copying column lists into prompts or
guessing from a live database. The explicit contracts diagnostic is the only
normal schema-inspection path.
