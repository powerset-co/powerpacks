# execute_role_search

Execute a role search using the role vertical contract.

This is the underlying retrieval primitive. In the slice-planning flow, it
should usually be called through `execute_search_slice`.

Expected inputs:

- `semantic_query`
- optional `bm25_queries`
- optional location filters
- optional `company_ids`
- optional `education_ids`
- optional `degree_levels`
- optional `seniority_bands`
- optional `role_tracks`
- optional `years_experience_min`
- optional `years_experience_max`
- optional `age_min`
- optional `age_max`
- optional `position_after_date`
- optional `position_before_date`
