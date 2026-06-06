---
name: search-network-jd
description: Run the complex JD recruiter loop for Powerpacks when the user provides a job posting URL, pasted job description, or broad multi-trait role brief. Fetch/read job URLs, build a bounded multi-probe search plan, ask once to execute or modify, then run approved probes and fan-out with budget accounting.
---

# Search Network JD

Use this only for job posting URLs, pasted job descriptions, or broad role briefs
where one search would likely be too noisy or miss distinct candidate patterns.

## Intake

If the input is a URL, fetch/read the page first. Persist the source URL, title,
and extracted text or summary under the current run/search directory. Do not
infer search criteria from the URL string alone.

Use the complex route when the fetched/pasted content has several of:

- title, responsibilities, qualifications, department, location, compensation,
  or hiring-process text
- hard filters plus soft filters
- OR-style experience families
- many nice-to-have skills
- founder-capable / CTO / technical cofounder / archetype language
- company-cohort examples that should seed separate searches

## Plan Preview

Create a durable multi-query plan. Show one compact preview with these literal
labels:

- `route`: `complex-JD recruiter loop`
- `normalized_archetype`
- `source_url` and `source_title`, when available
- `hard_filters`
- `initial_probes`: about 5 bounded probes with candidate limits
- `llm_review_budget`: default `100` unique profiles unless the user specified
  another budget
- `planned_review_count`
- `remaining_review_budget`
- `usable_cutoff`: `score >= 0.3`
- `cluster_plan`
- `fan-out`
- `lineage_state`

Ask exactly:

`Execute this search plan or modify it?`

## Execution

If the user chooses `execute`, run the approved initial probes. Prefer one
sub-agent per probe when the harness supports workers; otherwise run sequentially.

Each probe must:

- run `search_network_pipeline.py prepare` for its exact probe query
- run the returned `execute_command` without another user approval
- return only probe id, status, blocker if any, artifact directory, state path,
  found count, and a few top candidates/reasons

The main agent owns merge/dedupe, budget accounting, exemplar selection,
criteria mutations, fan-out planning, and final presentation.

## Budget And Lineage

- Only profiles actually sent to LLM review/rerank consume budget.
- Do count-only and retrieval-only gates before spending review budget.
- Drop `score < 0.3` from usable candidates; keep below-cutoff rows only for
  audit/debug artifacts.
- Store plan revisions, candidate feedback, criteria mutations, exemplar sets,
  fanout threads, and child run links in task state/lineage.
- For feedback-driven follow-ups, append candidate feedback first, then criteria
  mutation, then search plan revision, then run new probes.

## Final Summary

Return one compact final summary:

- `<N> found`
- `Run artifacts: <artifact-dir>`
- top candidates with score and reason
- remaining budget, if relevant

Read final candidate details from the `csv` path in each completed child run's
`artifacts` object. Other run files are internal and should be inspected only
for debugging.
