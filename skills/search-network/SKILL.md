---
name: search-network
description: Run a role-first people search from a natural-language query, job description, or URL. Use when the user wants the agent to decompose the request, choose a search strategy, retrieve candidates from TurboPuffer, review the frontier, and hydrate the best results without using expensive scoring.
---

# Search Network

Run the full Powerpacks search loop from one user request such as:

- `/search-network who are software engineers in sf`
- `/search-network senior engineers at series a fintech companies`
- `/search-network stanford engineers with 3-5 yoe in new york`

## Intent

Give the user one operational entrypoint that does the work end to end.

The agent should:

- create a unique JSON task run before retrieval
- decompose the request into the Powerpacks role-search schema
- decide whether adjacent/domain search is off, confirmed, or should be asked
  about
- decide whether to search directly, count first, or generate slices
- ask the user to choose `search only`, `rerank`, or requested changes before
  executing retrieval
- execute retrieval through TurboPuffer
- review the candidate frontier after each step
- hydrate the full candidate frontier through Postgres before LLM filtering
- filter clearly bad candidates with the conservative LLM filter when enabled
- if the user chooses `rerank` at approval, prepare sharded agentic review and
  reduce shard outputs into one sorted review artifact
- persist CSV/JSONL result artifacts for refinement and interoperability

## Strategy Loop

1. Create task state from `powerpacks/tasks/search-network.task.json`.
   Use `task_state.py init --query "<query>"` so the default run file is
   unique under `.powerpacks/runs/`.
2. Expand the request with `expand_search_request`.
3. Record the expansion output.
4. If the request mentions adjacency or domain intent, run
   `plan_adjacency_search` and record whether to include adjacency, ask the
   user, or stay strict.
5. Choose the initial strategy with `decide_search_strategy`.
6. Resolve ID-producing constraints before retrieval:
   - run `resolve_education` when `education_names` or unresolved school names
     are present
   - run `resolve_investors` when `investor_names` or unresolved investor names
     are present
   - run `resolve_companies` when company names or company attributes must
     become company IDs
   - run `apply_prefilters` when education, tech skills, social/interaction
     metrics, or large company intersections must become `base_candidate_ids`
7. Run one of:
   - direct role search
   - count then search
   - multi-slice retrieval
8. Record every primitive output into task state.
9. Assess the frontier with `assess_frontier`.
10. Decide the next action with `plan_candidate_review`.
11. Hydrate the full candidate frontier with `hydrate_people`.
12. If LLM filtering is enabled, run `llm_filter_candidates` after hydration.
13. Persist CSV/JSONL artifacts with `persist_search_results`.
14. If approval recorded `execution_mode = "rerank"`, run
    `agentic_candidate_review prepare`, dispatch shard review through the host
    harness, then run `agentic_candidate_review reduce --write-state`.
15. Present artifact paths, a compact result summary, and recommended follow-up
    refinements.
16. Stop when the frontier is coherent enough to present.

## First Response Contract

For an initial `/search-network ...` request, do not reply with only a short
acknowledgement such as "kicking off the search." The first response must give
the user operational status:

- the state file path, or the exact state path you are about to create
- whether this is direct, count-first, or sliced
- the hard filters and prefilters you plan to use
- whether any runtime dependency or credential is missing
- the proposed next step
- the approval choices: `search only`, `rerank`, or requested changes

Do not execute TurboPuffer retrieval, Postgres hydration, LLM filtering, package
installation, or credential setup before this approval gate unless the current
task state already records `approval.status = "approved"`.

Before showing the approval prompt, perform this payload quality gate:

- `role_search_filters.semantic_query` must be dense semantic retrieval prose,
  not a title or keyword phrase.
- It must be at least 80 characters and should usually be 2-3 concise
  sentences when there is role or domain intent.
- It must describe responsibilities, skills, scope, domain work, or profile
  evidence.
- It must not be identical to, or merely a singular/plural variant of, any
  `bm25_queries` entry.
- If this gate fails, do not ask for approval. Regenerate the decomposition
  first and explain that the first payload was invalid.

## Rules

- do not force slices when the query is already specific
- use counts and frontier feedback to decide whether to widen or narrow
- produce a short decision trace after each stage
- keep role, location, seniority, education, yoe, age, and company constraints
  explicit
- use `education_names` for school names that are not already canonical IDs,
  then run `resolve_education` before `apply_prefilters`
- make `semantic_query` a dense retrieval description, not a title. It should
  be 2-3 natural-language sentences describing what the person does, the work
  they are likely responsible for, and the experience/profile that would make
  them relevant. Put terse title aliases in `bm25_queries`, not
  `semantic_query`.
- never run retrieval with a short semantic query such as `"software engineer"`,
  `"senior engineer"`, `"product manager"`, `"founder"`, or any other title
  phrase. Those belong only in `bm25_queries`.
- use only documented filter fields, operators, and enum values
- consult `powerpacks/contracts/` before using Postgres columns or TurboPuffer
  attributes; do not discover live schema during normal search execution
- resolve raw company names before execute-time company filtering
- resolve raw investor names before company investor filtering
- use `company_semantic_queries` for company-domain intent such as database
  companies, AI infrastructure companies, developer tooling companies, fintech
  startups, healthcare providers, or other vertical/domain descriptions; do not
  rely on coarse `sector_types` alone for narrow company domains
- when `company_semantic_queries` and `sector_types` are both present,
  choose `company_sector_strategy` explicitly:
  - `staged` by default for ambiguous domain searches; start with hard sector
    intersection, then broaden if the company frontier is too small
  - `soft_union` for recall-heavy searches where labels are incomplete; semantic
    company results OR sector-filter companies
  - `hard_filter` when the user wants the sector as a strict constraint
  - `semantic_only` when the coarse sector labels are likely noisier than the
    semantic company query
  Expect `soft_union` and broadened `staged` searches to produce larger company
  ID sets; run `apply_prefilters` before people retrieval.
- preserve slice reasons and provenance when slices are used
- make slice knobs explicit when slicing:
  - title strictness
  - geography strictness
  - seniority strictness
  - currentness
  - company strictness
  - adjacency mode
  - hard filter expression
  - prefilter execution plan
  - candidate limit
  - hydration limit
- record hard filters as an executable expression, not as source labels
- distinguish base-ID prefilter plans from normal TurboPuffer filters;
  education, tech skills, social metrics, interaction metrics, and large
  company intersections can narrow the base-ID set before role search
- treat tenure/date windows as overlapping-position filters, not just start-date
  filters
- include company-domain adjacency only when explicitly requested, confirmed by
  the user, or recorded as a separate exploratory slice
- use persisted task state and artifacts as the source of truth; do not paste
  the full candidate set into chat
- do not run expensive scoring in V1
- do not run sharded agentic candidate review unless approval records
  `execution_mode = "rerank"` or the user explicitly asks to rerank/review a
  completed run
- when asking for approval, offer `search only` and `rerank` as first-class
  choices. `search only` runs retrieval, hydration, and normal persistence.
  `rerank` runs those steps plus sharded agentic review after hydration.
- treat `approve` as `search only` for backwards compatibility
- when sharded review is used, final user-facing output must be
  `ranked_candidates.csv` and `ranked_candidates.jsonl` from the reducer, not
  individual shard outputs
- after `agentic_candidate_review prepare`, use the task JSON's
  `artifacts.agentic_candidate_review.shards` as the dispatch source; do not
  rediscover shards from the filesystem
- after `agentic_candidate_review reduce`, present final ranked artifact paths
  from `artifacts.agentic_candidate_review`
- after planning, use `task_state request-approval` before real retrieval
- if the user chooses search only or says approve, use
  `task_state approve --execution-mode search_only`
- if the user chooses rerank, use `task_state approve --execution-mode rerank`
- if the user asks for changes, use `task_state request-changes --note "<user instruction>"`
- do not write new retrieval scripts during a search run. Use the packaged
  primitives under `powerpacks/primitives/`.

## Decision Heuristics

- use direct search when the query is already narrow and explicit
- use count-then-search when the query is clear but maybe broad
- use slices when one query likely misses good adjacent title or geography
  variants
- after a count, if the strict pool is over 1,000 unique people, normally plan
  slices before presenting results unless there is a clear reason a single top
  200 frontier is better for the user's immediate request
- when resolved company IDs exceed roughly 500, run `apply_prefilters` before
  role retrieval so people search batches `company_id` filters instead of
  sending one giant `In` clause to TurboPuffer
- useful default slice knobs for broad role/geography searches are seniority
  (`entry/junior/mid`, `senior/staff/principal`, `manager_plus`) and title
  strictness (`exact`, `close_variants`). Do not slice by company/domain unless
  the query asks for it or the user approves adjacency.
- ask before company-domain adjacency when it changes the meaning of the query
  and the user did not ask for adjacent candidates
- stop and present when the frontier is already coherent
- hydrate the full frontier before LLM filtering; use filtering/persistence to
  narrow what is presented
- persist results even when only candidate IDs are available

## Semantic Query Guidance

`semantic_query` is for vector retrieval over profile/role context. Describe
what the target person does: responsibilities, skills, scope, domain, and
profile signals that matter for matching. Do not use a bare title or keyword
phrase.

Keep examples out of the active prompt unless needed. If you need calibration,
read `powerpacks/docs/semantic-query-examples.md`, choose the closest pattern,
and adapt it to the user's actual query.

## Decision Trace

For every run, keep a JSON trace with:

- expanded constraints
- strategy decision and reason
- retrieval path used
- counts or per-slice yield
- frontier assessment
- next action and reason
- artifact paths for CSV, JSONL, manifest, and task state

The trace is part of the result. It lets the user inspect why the claw searched
that way.

Use `powerpacks/primitives/task_state/task_state.py` when a local filesystem is
available and include the state-file path in the final response. If not, keep
the same shape in the final response.

## Executable Commands

After approval, use the packaged primitive scripts rather than writing ad hoc
code:

```bash
python powerpacks/primitives/resolve_education/resolve_education.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/resolve_companies/resolve_companies.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/resolve_investors/resolve_investors.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/apply_prefilters/apply_prefilters.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/count_candidates/count_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/execute_role_search/execute_role_search.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --limit 200 \
  --write-state
```

```bash
python powerpacks/primitives/execute_search_slice/execute_search_slice.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --slice-id <slice-id> \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/hydrate_people/hydrate_people.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

```bash
python powerpacks/primitives/persist_search_results/results_io.py export \
  --state .powerpacks/runs/search-network-<id>.json
```

## Artifact Review

After persisting artifacts, present the task state path plus the CSV, JSONL,
and manifest paths. Use those files for refinement instead of trying to keep
the full candidate set in chat.

When the user asks to refine, filter, export, tag, or build on prior results,
use `refine_search_results` with the persisted JSONL/manifest and write a child
artifact rather than mutating the original run.

## Primary Primitives

- `expand_search_request`
- `task_state`
- `plan_adjacency_search`
- `decide_search_strategy`
- `resolve_education`
- `resolve_companies`
- `apply_prefilters`
- `count_candidates`
- `execute_role_search`
- `generate_search_slices`
- `execute_search_slice`
- `merge_candidate_frontier`
- `assess_frontier`
- `plan_candidate_review`
- `hydrate_people`
- `llm_filter_candidates`
- `agentic_candidate_review`
- `persist_search_results`
- `refine_search_results`

## Helper References

The installed user-facing skill is `search-network`. Do not require the user to
invoke helper skills directly.

When needed, consult these reference files in the installed repo:

- `powerpacks/docs/workflows/query-decomposition.md`
- `powerpacks/docs/workflows/role-search.md`
- `powerpacks/docs/workflows/slice-search.md`
- `powerpacks/docs/workflows/candidate-review-planning.md`
- `powerpacks/docs/workflows/turbopuffer-schema-guard.md`
- `powerpacks/docs/workflows/postgres-hydration.md`

## Source Of Truth

- `powerpacks/docs/search-surface.md`
- `powerpacks/docs/expand-execute.md`
- `powerpacks/docs/task-harness.md`
- `powerpacks/docs/slice-planning.md`
- `powerpacks/docs/semantic-query-examples.md`
- `powerpacks/docs/turbopuffer-contract.md`
- `powerpacks/docs/turbopuffer-schema.md`
- `powerpacks/docs/postgres-contract.md`
- `powerpacks/contracts/README.md`
- `powerpacks/contracts/postgres/persons.table.json`
- `powerpacks/contracts/turbopuffer/people.namespace.json`
- `powerpacks/contracts/turbopuffer/schools.namespace.json`
- `powerpacks/contracts/profiles/hydrated-profile.schema.json`
- `powerpacks/tasks/search-network.task.json`
- `powerpacks/schemas/search-network-task.schema.json`
- `powerpacks/schemas/task-run.schema.json`
- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`
- `powerpacks/schemas/search-slice.schema.json`
- `powerpacks/schemas/llm-filter-candidates.schema.json`
- `powerpacks/schemas/adjacency-plan.schema.json`
- `powerpacks/schemas/search-strategy-decision.schema.json`
- `powerpacks/schemas/frontier-assessment.schema.json`
- `powerpacks/schemas/candidate-review-plan.schema.json`
