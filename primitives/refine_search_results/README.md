# refine_search_results

Build on a previous search run instead of starting from scratch.

Inputs:

- prior task-state path or artifact manifest
- user refinement request
- optional selected `person_id` values from CSV, JSONL, or host review events

Common refinements:

- narrow by title, seniority, company, location, education, tenure, YOE, age,
  social metrics, or interaction thresholds
- widen with a new adjacent slice
- hydrate more of the existing frontier
- exclude selected candidates or companies
- create a follow-up search from high-performing slices

Rules:

- Load the previous run state and artifact manifest first.
- Reuse the original decomposition, hard-filter expression, slice provenance,
  and frontier IDs.
- Record a new task run with `parent_task_id` or a note linking to the prior
  run.
- Never mutate the prior CSV/JSONL in place; write a new artifact set.
- If the user says "based on that search", use the most recent run only when it
  is unambiguous.
