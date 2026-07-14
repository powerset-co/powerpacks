# TurboPuffer Contract

For the fixed namespace and attribute inventory, see the
[TurboPuffer schema index](turbopuffer-schema.md) and checked-in
[`contracts/turbopuffer/`](../contracts/turbopuffer/) JSON.

This document exists to stop agents from guessing field names and types.

## Role Search Filter Contract

Use these person-level fields and operators only:

| Field | Type | Operators | Notes |
| --- | --- | --- | --- |
| `city` | string | `In` | scalar location field |
| `state` | string | `In` | scalar location field |
| `country` | string | `In` | scalar location field |
| `metro_areas` | string[] | `ContainsAny` | array-backed field |
| `macro_region` | string | `In` | scalar location field |
| `seniority_band` | string | `In`, `NotIn` | enum-backed |
| `company_id` | string | `In` | canonical company IDs only |
| `is_current` | boolean | `Eq` | current position filter |
| `total_years_experience` | number | `Gte`, `Lte` | numeric range |
| `education_ids` via base-ID prefilter | string[] | `In` | school/alumni constraint |
| `inferred_birth_year` | number | `Gte`, `Lte` | age proxy after expand maps age to birth-year bounds |
| `start_date_epoch` | integer | `Lte` | tenure/date overlap logic |
| `end_date_epoch` | integer | `Gte`, `Eq` | tenure/date overlap logic |
| `role_track` | string | `In` | enum-backed |
| `role_ids` | string[] | `ContainsAny` | array-backed role IDs |
| `base_id` | string | `In` | person IDs used for hydration/prefilter |
| `allowed_operator_ids` | string[] | `ContainsAny` | private set-scope filter; not a user-authored constraint |

## Company Constraint Contract

Use these company-level concepts only as constraints inside role search:

| Field | Type | Notes |
| --- | --- | --- |
| `company_ids` | string[] | preferred once resolved |
| `company_names` | string[] | expand-time only; resolve before execute |
| `company_cities` | string[] | company location constraint |
| `company_states` | string[] | company location constraint |
| `company_countries` | string[] | company location constraint |
| `company_metro_areas` | string[] | company location constraint |
| `company_macro_regions` | string[] | company location constraint |
| `entity_types` | string[] | company-type enums |
| `sector_types` | string[] | sector enums |
| `technology_types` | string[] | company technology tags |
| `customer_types` | string[] | customer-type enums |
| `investors` | string[] | canonical investor IDs once resolved |
| `yc_batches` | string[] | YC batch labels |
| `funding_stage_min` | string | lower funding-stage bound |
| `funding_stage_max` | string | upper funding-stage bound |
| `funding_amount_min` | number | min funding amount |
| `funding_amount_max` | number | max funding amount |
| `headcount_min` | integer | min company headcount |
| `headcount_max` | integer | max company headcount |
| `last_funding_before` | string | ISO-like date string |
| `last_funding_after` | string | ISO-like date string |
| `valuation_min` | number | min valuation |
| `valuation_max` | number | max valuation |
| `founded_year_min` | integer | min founded year |
| `founded_year_max` | integer | max founded year |

## Operator Rules

- `In` is for scalar categorical fields
- `ContainsAny` is for array-backed fields
- `Eq` is for booleans
- `Gte` and `Lte` are for numeric bounds
- do not invent company filter families that are not in the current role-search schema

## Public Enum Hints

Common `seniority_bands`:

- `entry`
- `junior`
- `mid`
- `senior`
- `staff`
- `principal`
- `manager`
- `director`
- `vice_president`
- `c_suite`

Common `role_tracks`:

- `engineering`
- `data`
- `product`
- `design`
- `sales`
- `marketing`
- `operations`
- `business_dev`
- `finance`
- `strategy`

Supported recall-style constraints in the public role payload:

- `education_ids`
- `education_names`
- `degree_levels`
- `years_experience_min`
- `years_experience_max`
- `age_min`
- `age_max`
- `position_after_date`
- `position_before_date`

Supported company-side parity constraints in the public role payload:

- `entity_types`
- `sector_types`
- `technology_types`
- `customer_types`
- `investors`
- `yc_batches`
- `funding_stage_min`
- `funding_stage_max`
- `funding_amount_min`
- `funding_amount_max`
- `headcount_min`
- `headcount_max`
- `last_funding_before`
- `last_funding_after`
- `valuation_min`
- `valuation_max`
- `founded_year_min`
- `founded_year_max`

## Example

For "who are software engineers in sf" a safe role-search payload is:

```json
{
  "semantic_query": "Builds and maintains software products or systems, implements features, debugs issues, reviews code, and contributes to technical design. Works hands-on with application, backend, frontend, mobile, platform, infrastructure, or systems code in production environments.",
  "bm25_queries": ["software engineer", "software developer", "SWE", "software engineering"],
  "cities": ["San Francisco"],
  "states": ["California"],
  "role_tracks": ["engineering"],
  "is_current": true
}
```

For "software engineers from Stanford with 3-5 yoe in sf" a safe role-search
payload is:

```json
{
  "semantic_query": "Builds and maintains software products or systems, implements features, debugs issues, reviews code, and contributes to technical design. Works hands-on with application, backend, frontend, mobile, platform, infrastructure, or systems code in production environments.",
  "bm25_queries": ["software engineer", "software developer", "SWE", "software engineering"],
  "cities": ["San Francisco"],
  "role_tracks": ["engineering"],
  "education_ids": ["resolved-school-id"],
  "years_experience_min": 3,
  "years_experience_max": 5,
  "is_current": true
}
```

For "senior engineers at series a fintech companies with 50-200 employees" a
safe role-search payload is:

```json
{
  "semantic_query": "Owns meaningful technical areas, leads design or architecture decisions, mentors other engineers, and still contributes to production software systems. Shows responsibility for complex technical projects, platform or application reliability, and execution beyond entry-level implementation work.",
  "role_tracks": ["engineering"],
  "seniority_bands": ["senior", "staff", "principal"],
  "sector_types": ["fintech"],
  "funding_stage_min": "series_a",
  "funding_stage_max": "series_a",
  "headcount_min": 50,
  "headcount_max": 200,
  "is_current": true
}
```
