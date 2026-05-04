# apply_prefilters

Resolve hard prefilter stages to `base_id`s before role retrieval.

This is the bridge between recall-style constraints and role search. It handles
education, tech-skill, social/interaction, and company-set intersections, then
writes `base_candidate_ids` into task state for `count_candidates` and
`execute_role_search`.
