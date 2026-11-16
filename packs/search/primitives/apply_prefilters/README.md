# apply_prefilters

Resolve hard prefilter stages to `base_id`s before role retrieval.

This is the bridge between recall-style constraints and role search on the local
index. It handles education, tech-skill and company-set intersections, then
writes `base_candidate_ids` into task state for `execute_role_search`. A set
search does not use it: the Powerset API applies those filters itself.
