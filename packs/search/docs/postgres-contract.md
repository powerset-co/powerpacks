# Postgres Contract

Powerpacks does not introspect database columns at runtime. Primitives rely on
this checked-in contract.

## Required Credentials

Provide either:

- `DATABASE_URL`
- `SUPABASE_DATABASE_URL`
- `SUPABASE_DB_URL`
- or `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`,
  `POSTGRES_PASSWORD`

`POSTGRES_SSLMODE` defaults to `require`.

## Hydration Tables

### `persons`

Required columns:

- `id`
- `public_identifier`
- `public_profile_url`
- `full_name`
- `headline`
- `summary`
- `profile_picture_url`
- `location_raw`
- `city`
- `state`
- `country`
- `hydrated_context`
- `x_twitter_handle`
- `x_twitter_followers`
- `linkedin_followers`
- `linkedin_connections`
- `ig_handle`
- `ig_followers`
- `inferred_birth_year`

`persons.hydrated_context` is the source of truth for full profile context.
It must contain, when available:

- `positions[]`
- `education[]`
- `tech_skills[]`
- `linkedin_url`
- `name`
- `headline`
- `location`

### `person_source_summary`

Optional. If present, Powerpacks reads:

- `person_id`
- `total_interactions`

### `companies`

Required when company backfill or company detail lookup is used:

- `id`
- `name`
- `harmonic_urn`
- `domain`
- `website_domain`
- `linkedin_url`
- `description`
- `headcount`
- `funding_total`
- `funding_stage`
- `entity_types`
- `sector_types`
- `founded_year`
- `location`
- `li_company_id`
