# TurboPuffer Schema

Powerpacks uses fixed TurboPuffer namespace and attribute contracts. Do not
query namespace schemas on every run.

## Credentials

- `TURBOPUFFER_API_KEY`
- `TURBOPUFFER_REGION`, default `gcp-us-central1`
- optional `ALEPH_ENV` suffixes namespaces for non-prod environments

## Namespaces

- people: `aleph_people_v1`
- education: `aleph_people_education_v1`
- summaries: `aleph_summaries_v1`
- companies: `aleph_companies_v1`
- investors: `aleph_investors_v1`

## People Namespace Attributes

The people namespace is position-level. IDs may be position IDs; `base_id`
identifies the person.

- `id`
- `base_id`
- `position_title`
- `word_tokens`
- `char_tokens`
- `d2q_tokens`
- `phrase_tokens`
- `city`
- `state`
- `country`
- `macro_region`
- `metro_areas`
- `seniority_band`
- `company_id`
- `is_current`
- `total_years_experience`
- `start_date_epoch`
- `end_date_epoch`
- `role_track`
- `allowed_operator_ids`
- `role_ids`
- `inferred_birth_year`
- `x_twitter_followers`
- `linkedin_followers`
- `linkedin_connections`
- `ig_followers`

## Supported People Filters

- `city In`
- `state In`
- `country In`
- `macro_region In`
- `metro_areas ContainsAny`
- `seniority_band In` / `NotIn`
- `company_id In`
- `is_current Eq`
- `total_years_experience Gte` / `Lte`
- `start_date_epoch Lte`
- `end_date_epoch Gte` / `Eq`
- `role_track In`
- `allowed_operator_ids ContainsAny`
- `role_ids ContainsAny`
- `base_id In`
- `inferred_birth_year Gte` / `Lte`
- `x_twitter_followers Gte` / `Lte`
- `linkedin_followers Gte` / `Lte`
- `linkedin_connections Gte` / `Lte`
- `ig_followers Gte` / `Lte`

Date-window tenure filters use overlap semantics:

- `start_date_epoch <= period_end`
- `end_date_epoch >= period_start OR end_date_epoch == 0`

## Investor Resolver Namespace

The investor namespace is the source of truth for resolving investor names into
company/person URNs used by company `investor_urns` filters.

- `id`: investor URN, or alias row ID
- `canonical_urn`: canonical investor URN for alias rows
- `investor_name`: exact display name
- `investor_name_tokens`: full-text token field
- `investor_type`: `company` or `person`
- `investment_count`: popularity/ranking count

Build or refresh it with:

```bash
python3 primitives/build_investor_index/build_investor_index.py \
  --csv /path/to/data/investors/investors_full.csv \
  --env-file /path/to/.env
```
