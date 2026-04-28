# Add Postgres Hydration

Install and maintain Postgres-backed hydration after search retrieval.

## Intent

Use Postgres for:

- retrieving canonical profile details
- fetching richer person/company metadata after search
- paging or stitching together result details

Do not use it as the primary free-text search path in V1.

## Rules

- TurboPuffer retrieves candidates
- Postgres hydrates and formats them
- keep hydration keys stable and explicit
- if search and hydration IDs disagree, return the mismatch clearly instead of
  silently falling back

## Primary Primitives

- `hydrate_people`
- `query_postgres_profiles`
