# plan_candidate_review

Review a merged candidate frontier without running expensive scoring.

Expected inputs:

- merged frontier
- per-slice counts
- overlap statistics
- decomposition context

Expected outputs:

- `next_action`
- `frontier_size`
- `suggested_hydration_count`
- reasons and notes

Allowed next actions:

- `hydrate_frontier`
- `narrow_filters`
- `widen_with_new_slice`
- `run_additional_direct_query`
- `ask_for_clarification`
- `stop_and_present`

Do not run expensive scoring in V1.
