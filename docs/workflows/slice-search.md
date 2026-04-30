# Slice Search

Define multi-slice retrieval planning for role-first people search.

## Intent

Avoid one giant retrieval pass when the user starts with an open-ended query.

## Rules

- prefer 3-8 slices
- give every slice an explicit reason
- vary title, geography, seniority, currentness, or company strictness with
  purpose
- preserve slice provenance across merge
- do not run expensive scoring in V1

## Primary Primitives

- `expand_search_request`
- `generate_search_slices`
- `resolve_education`
- `resolve_companies`
- `apply_prefilters`
- `count_candidates`
- `execute_search_slice`
- `merge_candidate_frontier`
- `plan_candidate_review`
