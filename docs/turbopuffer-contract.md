# TurboPuffer Contract

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
| `allowed_operator_ids` | string[] | `ContainsAny` | keep private; not part of public V1 |

## Company Constraint Contract

Use these company-level concepts only as constraints inside role search:

| Field | Type | Notes |
| --- | --- | --- |
| `company_ids` | string[] | preferred once resolved |
| `company_names` | string[] | expand-time only; resolve before execute |

## Operator Rules

- `In` is for scalar categorical fields
- `ContainsAny` is for array-backed fields
- `Eq` is for booleans
- `Gte` and `Lte` are for numeric bounds
- do not invent company filter families that are not in the public V1 schema

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
- `degree_levels`
- `years_experience_min`
- `years_experience_max`
- `age_min`
- `age_max`
- `position_after_date`
- `position_before_date`

## Example

For "who are software engineers in sf" a safe role-search payload is:

```json
{
  "semantic_query": "software engineer",
  "bm25_queries": ["software engineer", "software developer"],
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
  "semantic_query": "software engineer",
  "bm25_queries": ["software engineer", "software developer"],
  "cities": ["San Francisco"],
  "role_tracks": ["engineering"],
  "education_ids": ["resolved-school-id"],
  "years_experience_min": 3,
  "years_experience_max": 5,
  "is_current": true
}
```
