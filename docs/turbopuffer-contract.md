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
| `role_track` | string | `In` | enum-backed |
| `role_ids` | string[] | `ContainsAny` | array-backed role IDs |
| `base_id` | string | `In` | person IDs used for hydration/prefilter |
| `allowed_operator_ids` | string[] | `ContainsAny` | keep private; not part of public V1 |

## Company Search Filter Contract

Use these company-level concepts in the public V1 payload:

| Field | Type | Notes |
| --- | --- | --- |
| `company_ids` | string[] | preferred once resolved |
| `entity_types` | string[] | company-type enums |
| `sector_types` | string[] | company-sector enums |
| `funding_stage_min` | string | enum-like stage token |
| `funding_stage_max` | string | enum-like stage token |
| `company_cities` | string[] | company location |
| `company_states` | string[] | company location |
| `company_countries` | string[] | company location |
| `company_metro_areas` | string[] | company location |
| `company_macro_regions` | string[] | company location |

## Operator Rules

- `In` is for scalar categorical fields
- `ContainsAny` is for array-backed fields
- `Eq` is for booleans
- `Gte` and `Lte` are for numeric bounds
- do not mix company location fields with person location fields

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
