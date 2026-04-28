---
name: add-slice-search
description: Install and maintain multi-slice retrieval planning for role-first people search. Use when search should decompose one request into several bounded retrieval slices, execute them independently, and merge the frontier before hydration.
---

# Add Slice Search

Install and maintain multi-slice retrieval planning for role-first people
search.

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
- `resolve_companies`
- `count_candidates`
- `execute_search_slice`
- `merge_candidate_frontier`
- `plan_candidate_review`
