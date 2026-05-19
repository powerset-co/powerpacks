---
name: search-network
description: Run a role-first people search from a natural-language query, job description, or URL. Use when the user wants the agent to decompose the request, show one compact search preview, then execute retrieval, hydration, LLM filtering/reranking, and result export.
---

# Search Network

Run the full Powerpacks search loop from one user request such as:

- `/search-network who are software engineers in sf`
- `/search-network senior engineers at series a fintech companies`
- `/search-network stanford engineers with 3-5 yoe in new york`
- `/search-network people who work at OpenAI`

## Intent

Give the user one operational entrypoint that does the work end to end.

The agent should:

- create a unique JSON task run before retrieval
- orchestrate helper skills when they produce a cleaner intermediate artifact
- decompose the request into the Powerpacks role-search schema
- decide whether adjacent/domain search is off, confirmed, or should be asked
  about
- decide whether to search directly, count first, or generate slices
- show one compact search preview after extraction and ask the user to
  `execute` or `modify`
- after `execute`, run retrieval, hydration, LLM filtering/reranking, and
  persistence without a second approval gate
- review the candidate frontier after each step
- hydrate the full candidate frontier through Postgres into local JSONL handoff
  files; do not pass large profile blobs through chat or command arguments
- run conservative LLM filtering by default after hydration using the handoff path
  and the cheaper filter model (`POWERPACKS_LLM_FILTER_MODEL`, default mini)
- run the async `llm_rerank_candidates` primitive by default after filtering,
  using the stronger configured rerank model (`--model`, default `gpt-5.4`) and
  producing `query_results.csv` with reasoning, confidence, and trait scores
- expect LLM filtering + reranking to legitimately take about 2-3 minutes for
  large searches; do not kill the run just because this step is quiet
- persist result artifacts in reranked order for refinement and interoperability

## Skill Composition

`search-network` is the high-level orchestration skill. It may sequence other
skills, but every handoff must be explicit and durable. Do not rely on chat
memory as the boundary between steps.

When a sub-step is covered by another installed skill:

- load that skill's instructions for that step
- give it a concrete input artifact or task-state path
- require a concrete output artifact or recorded task-state step
- feed that artifact into the next step

Exception: the normal `search-network` happy path should not browse helper
skills, schemas, docs, or contracts before doing work. Use the packaged
primitives below; consult references only when a primitive fails, emits a
schema/contract blocker, or the user asks for debugging.

Default composition:

- `expand_search_request` primitive: always use this first to produce
  the extraction JSON. It runs 7 parallel domain-specific extractors
  (role, company, location, education, temporal, seniority, social)
  via OpenAI, matching the app's battle-tested prompts.

  ```bash
  uv run --env-file .env --project . python packs/search/primitives/expand_search_request/expand_search_request.py \
    --query "<user query>" --env-file .env
  ```

  Do not use the `extract-search-query` skill for extraction — use the
  primitive directly. Do not hide query extraction inside eval or harness-only
  code paths. The skill is retained as documentation only.
- `search-company`: use when natural-language company criteria, investors,
  sectors, funding, headcount, or company-domain intent must resolve into
  canonical company IDs before people retrieval
- `fix-people`: use only when the user explicitly asks to reconcile, merge, or
  clean persisted person artifacts after search

Useful handoff artifacts:

- task state: `.powerpacks/runs/search-network-<id>.json`
- extracted query: recorded at `steps[].id = "expand_search_request"`
- planned checklist: recorded in `planned_steps[]` at approval time
- company resolution: recorded at `steps[].id = "resolve_companies"` and, when
  useful for debugging, mirrored to `artifacts/company_ids.json`
- set resolution: recorded at `steps[].id = "resolve_set_operators"` and
  consumed as `role_search_filters.operator_ids`
- candidate frontier: recorded by retrieval steps and exported as JSONL
- hydrated handoff: `hydrate_people` records `profiles_path` and
  `llm_profiles_path` in task state; agents should pass the state/path, not read
  the profile file unless the user asks for details
- rerank output: `artifacts/<task>/llm_rerank_candidates/query_results.csv`;
  the CSV columns match the app query-results schema (`person_id`,
  `result_index`, `matched_position_indexes`, `final_score`, `trait_scores`,
  `overall_reasoning`, plus metadata fields)

If a future workflow chains more skills, keep the same pattern: each task
consumes a path or task-state step and produces a path or task-state step.
Example: `search-company` -> `search-network` -> `fix-people` is valid only if
each boundary has a written artifact.

## Fast path runner

For the normal semantic/role search path, use the resumable orchestrator's
`prepare` command as the first action after loading this skill. It runs
`expand_search_request`, writes the payload artifact, performs the compact
quality gate, and emits the exact preview fields plus an `execute_command`.
Do **not** grep/search/read the repo, schemas, docs, primitive source, or prior
artifacts before this command unless the user explicitly asked to debug them.

Do not use `prepare` for the company-directory fast path below. Company-only
queries such as `people who work at OpenAI` should call `list_company_people`
directly and skip extraction, task state, retrieval, hydration, filtering, and
reranking.

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
  --query "<user query>"
```

Show the returned `preview` compactly and ask exactly:

`Execute this search or modify it?`

If the user chooses `execute`, run the returned `execute_command` exactly. It
already includes `--execute-approved`, so there is no second LLM approval gate.

For manual continuation, the equivalent run command is:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
  --query "<user query>" \
  --payload-json .powerpacks/search/<run>/expand_search_request.json \
  --execute-approved
```

`--execute-approved` means the user already saw and approved the extracted
search. The orchestrator should then run retrieval, hydration, LLM filtering,
LLM reranking, and persistence without another chat-visible approval gate.

Do not read the generated payload JSON, task state, ledger, hydrated profiles,
CSV, or JSONL just to summarize progress. The primitives emit compact preview,
status, and final summary objects for that. Read artifacts only for diagnosis,
refinement, or when the user asks to inspect details.

Do not mention alternate execution modes, LLM reranking, or skip-rerank options
in the user-facing preview. LLM filtering/reranking is the default execution
path. If query extraction or currentness semantics are still ambiguous, resolve
those before showing the preview or invoking the orchestrator.

### Quiet Execution

After query extraction/currentness is resolved and the user has selected
`execute`, do not create a separate chat-visible plan for normal orchestrator
runs. Keep invoking `search_network_pipeline.py` until it
finishes or emits a concrete `blocked_approval` / `blocked_user_action`.

When the harness has a worker/sub-agent facility, dispatch noisy orchestrator
execution to a worker sub-agent. The only chat-visible handoff line should be
exactly:

`Starting search through sub-agent.`

LLM filtering + reranking can legitimately take about 2-3 minutes. Treat the
primitive's starting/estimate line as progress, keep the worker alive, and do
not restart or cancel solely because no candidates are printed while the LLM
fan-out is running.

The main chat should show only required user actions and the final compact
result summary with one artifact directory, one user-facing found count, and top
candidates. Use `Run artifacts: <artifact-dir>` instead of listing state,
ledger, CSV, JSONL, manifest, and rerank paths separately. Use `N found`, not
internal stage counts such as retrieved, hydrated, filtered, reranked, or
exported. The worker may inspect the ledger, task state, primitive JSON, and
artifacts, but should not stream full primitive JSON or terminal transcripts
into the main chat unless diagnosis is needed.

If sub-agents are unavailable, say that once, then keep status messages
decision-oriented and terse.

## Strategy Loop

### Fast Path: Company Directory Lookup

For basic company-only requests like `$search-network people who work at Betr`,
`show me people at OpenAI`, `who works at Stripe?`, or `current employees of
Databricks`, do **not** run semantic people search. If the request has a
specified company and no specified role/title/seniority/domain/person filters,
route directly to the existing app company-directory API through MCP:

```text
list_company_people(company_name="OpenAI", page=0, page_size=50, company_limit=5)
```

This is a deterministic company → people directory lookup backed by the app's
`POST /v2/companies/search` with `include_people=true`. It is not role search,
not reranking, and not semantic retrieval.

Rules for this fast path:

1. Use `list_company_people` by `company_name` for the first call unless the user
   provided an exact company id, in which case pass `company_id`.
2. If multiple plausible companies are returned, ask the user to choose. Prefer
   an exact case-insensitive company-name match when obvious.
3. Page with `page` / `page_size`; do not auto-page through the whole company
   unless the user asks.
4. If the user explicitly asks to scope the company directory to a set, resolve
   the set with `list_sets` using the same tiebreaker as `search-contacts`
   (exact name → non-personal → most members → personal → ask), then pass
   `set_id` to `list_company_people`.
5. Skip task state, `extract-search-query`, `search-company`, `resolve_companies`,
   `count_candidates`, `execute_role_search`, `hydrate_people`, approval prompts,
   slicing, LLM filtering, rerank preparation, and result export.
6. If the user adds a role/title/seniority/domain constraint — e.g. "AI engineers
   at OpenAI", "VPs at Stripe", "founders at fintech startups" — this is no
   longer the company-directory fast path. Continue with the normal
   `search-network` workflow below.

This replaces the standalone `company-directory` skill; `search-network` is the
canonical entrypoint for both company-directory lookups and semantic/role people
search.

1. Create task state from `powerpacks/tasks/search-network.task.json`.
   Use `task_state.py init --query "<query>"` so the default run file is
   unique under `.powerpacks/runs/`.
2. Run the `expand_search_request` primitive to produce the extraction JSON.
   Save the output to `.powerpacks/search/<run>/expand_search_request.json`.
3. Record the expansion output.
4. If the request mentions adjacency or domain intent, run
   `plan_adjacency_search` and record whether to include adjacency, ask the
   user, or stay strict.
5. Choose the initial strategy with `decide_search_strategy`.
6. Resolve set scoping before retrieval:
   - if the user provides `set_id`, preserve it in `role_search_filters.set_id`
   - otherwise allow `resolve_set_operators` to use
     `POWERPACKS_DEFAULT_SET_ID` / `POWERSET_DEFAULT_SET_ID`, or the logged-in
     operator's active personal set from `~/.powerpacks/credentials.json`
   - record the returned `operator_ids`; these are the values used for
     TurboPuffer `allowed_operator_ids`, not the raw set UUID
7. Resolve ID-producing constraints before retrieval:
   - run `resolve_education` when `education_names` or unresolved school names
     are present
   - run `resolve_investors` when `investor_names` or unresolved investor names
     are present
   - run `resolve_companies` when company names or company attributes must
     become company IDs
   - run `apply_prefilters` when education, tech skills, social/interaction
     metrics, or large company intersections must become `base_candidate_ids`
8. Run one of:
   - direct role search
   - count then search
   - multi-slice retrieval
9. Record every primitive output into task state. `steps[]` is the append-only
   execution log; `planned_steps[]` is the mutable checklist that should move
   from pending to completed/failed/skipped as matching steps run.
10. Assess the frontier with `assess_frontier`.
11. Decide the next action with `plan_candidate_review`.
12. Hydrate the full candidate frontier with `hydrate_people --write-state`.
    Do not dump full hydrated profiles unless the user explicitly asks to debug
    hydration; then pass `--dump-profiles`.
13. Run conservative LLM filtering by default:
    `llm_filter_candidates --state "$STATE" --profile-scope auto --write-state`.
    Auto uses compact/current-role profiles only when role filters are
    current-scoped (`is_current_role: true`); all-time/past-role queries use the
    full hydrated profile. Do not dump filter scores/prompts unless debugging; then
    pass `--dump-debug`. Use `--allow-partial-hydration` only when the user
    explicitly accepts partial review.
14. Run async LLM reranking by default with the stronger configured model:
    `llm_rerank_candidates --state "$STATE" --concurrency 200 --model gpt-5.4 --write-state`.
    Rerank is the final ordering pass and reads the full hydrated profile from
    `profiles_path`. The primary output is `llm_rerank_candidates/query_results.csv`; columns must match the app
    query-results schema: `conversation_id`, `query`, `person_id`,
    `result_index`, `matched_position_indexes`, `final_score`, `trait_scores`,
    `overall_reasoning`, `pre_rerank_score`, `tags`, `vertical_sources`,
    `created_at`.
15. Persist CSV/JSONL artifacts with `persist_search_results`; it should use
    `llm_rerank_candidates.output.ranked_candidate_ids` when present so the
    exported results are in final reranked order.
16. Present a compact result summary, one artifact directory, one user-facing
    found count, top candidates with score + reason, and recommended follow-up
    refinements. Do not list state, ledger, CSV, JSONL, manifest, or rerank
    paths individually; say `Run artifacts: <artifact-dir>`. Do not list
    internal stage counts; say `N found`.
17. Stop when the frontier is coherent enough to present.

## First Response Contract

For an initial `/search-network ...` request, do not reply with a long visible
plan. Extract the query, run the payload quality gate below, then show one
compact search preview and ask for exactly one decision:

`Execute this search or modify it?`

The preview should include only:

- normalized query
- state path or exact state path to create
- set scope: explicit `set_id`, env/default set, or personal-set fallback
- role/title intent only when a `semantic_query` or title/BM25 filters exist
- company/domain/investor/education/location/seniority filters that are present
- tenure/date window when present, phrased as a plain-language window such as
  `Roblox roles overlapping 2016-2020`
- runtime blockers, if any

Do not print absent/null schema fields. Do not explain that
`semantic_query`, `is_current_role`, or `is_current_company` is missing. If
there is no role/title intent, do not include a role/title section. If there is
a date window, show the window in human terms instead of listing currentness
internals.

If the user chooses `execute`, invoke `search_network_pipeline --execute-approved`
so there is no second LLM approval gate. If the user asks for changes,
update/regenerate the extraction and show the compact preview again.

Before showing the preview, perform this payload quality gate:

- If the query has role/profile/domain intent, `role_search_filters.semantic_query`
  must be dense semantic retrieval prose, not a title or keyword phrase.
- It must be at least 80 characters and should usually be 2-3 concise
  sentences when there is role or domain intent.
- It must describe responsibilities, skills, scope, domain work, or profile
  evidence.
- It must not be identical to, or merely a singular/plural variant of, any
  `bm25_queries` entry.
- For pure hard-filter queries with no role/profile/domain intent (for example,
  "people who worked at Meta after 2020"), `semantic_query` may be left out of
  the payload; the pipeline will use filter-only TurboPuffer retrieval after
  resolving companies and applying prefilters.
- If this gate fails, do not ask for approval. Regenerate the decomposition
  first and explain that the first payload was invalid.

## Rules

- default to LLM filtering and LLM reranking after the user approves the search
  preview; only skip them when `OPENAI_API_KEY` is missing or when the task is
  the company-directory MCP fast path
- do not force slices when the query is already specific
- use counts and frontier feedback to decide whether to widen or narrow
- produce a short decision trace after each stage
- keep role, location, seniority, education, yoe, age, and company constraints
  explicit
- preserve extractor-inferred seniority unless it is schema-invalid or directly
  contradicts the user's query. Do not delete `seniority_bands` just because the
  user did not name a seniority; the seniority extractor intentionally infers
  default IC bands for roles such as software engineers.
- make currentness explicit for every role or company query. Use only
  `is_current_role` and `is_current_company` in `role_search_filters`; do not
  emit legacy `is_current`. If the user's wording does not make currentness
  clear, ask before retrieval; do not proceed with implicit currentness.
- split semantics such as current company plus past role should use
  `is_current_company` / `is_current_role` instead of silently conflating them.
- use `education_names` for school names that are not already canonical IDs,
  then run `resolve_education` before `apply_prefilters`
- do not use broad role labels such as `engineering` as hard proxies for
  representative titles. For normal engineering/product/operator searches,
  express title intent in `semantic_query` + `bm25_queries` and inspect/resolve
  representative title strings when needed. Use `role_ids` only for canonical
  roles where the index semantics are known to be reliable (notably founder /
  co-founder style roles) or when a prior title/role resolution step returned
  explicit representative IDs.
- make `semantic_query` a dense retrieval description, not a title. It should
  be 2-3 natural-language sentences describing what the person does, the work
  they are likely responsible for, and the experience/profile that would make
  them relevant. Put terse title aliases in `bm25_queries`, not
  `semantic_query`.
- never run hybrid retrieval with a short semantic query such as `"software engineer"`,
  `"senior engineer"`, `"product manager"`, `"founder"`, or any other title
  phrase. Those belong only in `bm25_queries`. For pure hard-filter searches,
  omit `semantic_query` instead of inventing a generic short query.
- use only documented filter fields, operators, and enum values
- consult `powerpacks/contracts/` before using Postgres columns or TurboPuffer
  attributes; do not discover live schema during normal search execution
- include a `set_id` whenever the user provides one. If they do not provide
  one, run `resolve_set_operators` without `--set-id` so it inherits
  `POWERPACKS_DEFAULT_SET_ID` / `POWERSET_DEFAULT_SET_ID`, or falls back to the
  logged-in operator's active personal set. Record the returned `operator_ids`
  before retrieval.
- never pass a raw `set_id` as a TurboPuffer `allowed_operator_ids` value.
  `set_id` is a Powerset set UUID; `operator_ids` are the Auth0 user IDs from
  `set_members.user_id`.
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
- the normal approval vocabulary is `execute` or `modify`. `execute` runs
  retrieval, hydration, LLM filter, LLM rerank, and persistence; `modify`
  revises the extracted payload before any retrieval.
- do not run sharded agentic candidate review in the normal path. Use the
  packaged `llm_filter_candidates` and `llm_rerank_candidates` primitives via
  `search_network_pipeline --execute-approved`.
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

Use `powerpacks/packs/search/primitives/task_state/task_state.py` when a local filesystem is
available and include the state-file path in the final response. If not, keep
the same shape in the final response.

## Executable Commands

After the user approves the compact preview, use the packaged orchestrator rather
than writing ad hoc code or running bare `python` commands:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
  --query "<user query>" \
  --payload-json .powerpacks/search/<run>/expand_search_request.json \
  --execute-approved
```

For status/continuation diagnostics, keep using `uv run --env-file .env`:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py status \
  --ledger <ledger>
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
- `resolve_investors`
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
- `llm_rerank_candidates`
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
- `powerpacks/packs/search/schemas/task-run.schema.json`
- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`
- `powerpacks/schemas/search-slice.schema.json`
- `powerpacks/schemas/llm-filter-candidates.schema.json`
- `powerpacks/schemas/adjacency-plan.schema.json`
- `powerpacks/schemas/search-strategy-decision.schema.json`
- `powerpacks/schemas/frontier-assessment.schema.json`
- `powerpacks/schemas/candidate-review-plan.schema.json`
