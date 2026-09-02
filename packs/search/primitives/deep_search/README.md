# deep_search — the `$search` deep-mode engine

Created: 2026-09-02

Change log:
- 2026-09-02 (later): the opt-in exhaustive engine (robust-source, triage,
  per-trait judge, consensus core gate, anchor expansion, micro-sort, plan
  critic) and the Reflect bench were deleted. The pond harness is the only
  engine; `--mode` is gone.
- 2026-09-02: First version. Written from a full read of the package at
  powerpacks 1.25.1 (`511a5796`) so the next session can start here instead
  of rescanning the code. Function names are the anchors; line numbers rot.

One engine: **the pond harness**. One reviewed plan, then one broad candidate
population at a time, reranked, a company-fit panel over the top rows, a model
proposes the next pond, up to four ponds, merged into review groups. Retrieval
is the ordinary `../search_network_pipeline/` run.

```mermaid
flowchart TD
    JD[jd.txt from --jd-file or fetch_jd --jd-url] --> PLAN[build_eval_inputs<br/>plan.raw.json → epoch0/plan.json]
    PLAN --> FLOORS[network_floors.probe_populations<br/>network_floors.json]
    FLOORS --> Q1[decompose_jd<br/>pond-1 prompt + ≤1 move card → queries.json]
    Q1 --> REVIEW{Human: query line + filters line}
    REVIEW -->|--plan-approved| BIND[bind_approved_plan → plan_binding.json<br/>results.json + manifest.json]
    BIND --> COMPILE[compile-pond<br/>search_network_pipeline prepare: 8 extractors → payload<br/>terra pattern defaults + payload-edit cards]
    COMPILE --> RUN[run-pond<br/>prefilters → hybrid retrieval ≤1000 → hydrate<br/>llm_filter → llm_rerank final_score]
    RUN --> FIT[company-fit panel on rows ≥0.70 or ≥0.30<br/>role_fit · craft_and_potential · company_taste · move_feasibility → group]
    FIT --> DECIDE[decide<br/>next-pond prompt + ≤3 move cards]
    DECIDE -->|refine / add_adjacent_pond / widen_geography| COMPILE
    DECIDE -->|ranking_fix| FIT
    DECIDE -->|stop / corpus_sparse / pond 4| DONE[summary groups → shortlist.csv, relationship.csv]
```

## Stage by stage

| Stage | Code | Model call | Inputs | Output |
| --- | --- | --- | --- | --- |
| Intake | `deep_search_loop.main` → `fetch_jd.py` (subprocess when `--jd-url`) | none | URL (Ashby posting API special-cased) | `jd.txt`, `source.json`; JD under 400 chars is rejected |
| Plan | `search_harness.prepare_review` → `build_eval_inputs.py` | gpt-5.6-luna, medium; system = `expand_search_request/prompts/trait_generation.txt` + `DEEP_PLAN_ADAPTER_PROMPT` | full JD | `epoch0/plan.raw.json` verbatim; `plan_from_obj` normalizes into `epoch0/plan.json` (fields below) |
| Floors | `network_floors.probe_populations` | none (TurboPuffer `multi_query` grouped by `base_id`, or DuckDB count) | every `candidate_populations[]` except `ranking-boost` / `comp-band-anchor`, plus plan location | `network_floors.json`; counts feed the review text and the next-pond prompt, never the Pond-1 prompt |
| Pond-1 query | `decompose_jd.py` | gpt-5.6-luna, medium; system = `pond_prompts.load_pond_prompt(plan, "pond-1")` | full JD + `job_title`, `location`, `candidate_populations` + at most one move card from `precedents.retrieve_next_moves` (chain cut to its first link). **`must_have` / `nice_to_have` are not in this prompt**; core traits are only used to *retrieve* the card. | `queries.raw.json` (parsed response + the injected cards), `queries.json` — exactly one seed, location label appended |
| Review | `deep_search_loop` returns `awaiting_plan_approval` | none | human edits `epoch0/plan.json` / `queries.json` | — |
| Bind | `validate_approved_plan` → `resolve_retrieval_identity` → `bind_approved_plan` → `initialize_run` | none | plan, JD, queries, corpus identity | `plan_binding.json` (sha of plan + JD + queries, set id or DuckDB identity), `results.json` (`search-harness.v1`, `pending_query`), `manifest.json` |
| Compile pond | `search_harness.compile_pond` → `search_network_pipeline.py prepare` (subprocess) → `expand_search_request.py` | 8 parallel extractors, all gpt-5.6-luna (role, company, location, education, temporal, seniority, social, trait_generation); then `_llm_pattern_defaults` gpt-5.6-terra, medium | the pond query only; terra gets `{title, brief, target_level}`, the compiled payload, prior pool stats, ≤3 payload-edit cards — no JD | `ponds/pond-NN/prepare/expand_search_request.json`, `payload.json`, `pattern-defaults.raw.json`; plan location and filter contract are re-imposed by `apply_shared_plan_scope` |
| Review payload | `search_harness.review_payload` | none | edited `payload.json`, `--rerank-exclusion` | `human_edit_delta` in the iteration |
| Run pond | `search_harness.run_pond` → `search_network_pipeline.py run --execute-approved --limit 1000` | filter gpt-5.6-luna/none (batch 2); rerank gpt-5.6-luna/medium (one call per candidate, ≤400 concurrent) | payload; `--evaluation-query` = pond query + rerank exclusions | pipeline artifacts under `.powerpacks/runs/artifacts/<task>/`; rows sorted by `final_score` |
| Company-fit panel | `search_harness._annotate_company_fit` with prompts in `company_context.py` | gpt-5.6-luna, medium; 4 expert calls + 1 decision call per reviewed row; ≤400 concurrent; `FIT_ANNOTATION_LIMIT` 500 | `_fit_input`: full JD, `target_level`, `brief{occupation, defining_capability, geography}`, `comp_band`, hiring-company context (RapidAPI, cache-first), fit precedents, candidate (current role, ≤3 recent roles, education, `rerank_score`, `pond_trait_scores`). **`must_have`, `core_groups`, `nice_to_have` are not passed**; `defining_capability` is the core trait strings joined. | `ponds/pond-NN/company-fit/NNN-<expert>.json`, `NNN.json`; `shortlist_grades[]` in the iteration |
| Decide | `search_harness.decide` → `propose_next_move` | gpt-5.6-luna, medium; system = `load_pond_prompt(plan, "next-pond")` | title, hiring company, current query, frozen brief, pond chain, `candidate_populations`, floor labels, comp band, relaxation order, human diagnosis, ≤3 move cards, pool stats, ≤20 anonymized title/company pairs — no JD | `next_move{diagnosis, action, next_query, source, rationale}`, `proposal_delta`, `human_override`; then `pending_query`, a rerank-only `pending_payload`, or `completed` |
| Summary | `search_harness._save` → `export_search_summary` | none | every iteration, plus other runs of the same JD in the parent dir | `results.json.summary`, `manifest.json`; on completion `shortlist.csv`, `relationship.csv` |

## What ranks and what buckets

- **Rank inside a pond** = `final_score` from `llm_rerank_candidates` — the
  per-trait rubric over the pond query's `traits[]` (the ones
  `expand_search_request`'s `trait_generation` extractor produced). Those
  traits do not touch retrieval ordering; their only retrieval effect is the
  `is_current` filter via `apply_trait_currentness`.
- **Rows that get the panel** = `final_score ≥ 0.70`, else `≥ 0.30` when
  nothing clears 0.70 (`REVIEW_SCORE_THRESHOLD`, `FALLBACK_REVIEW_SCORE_THRESHOLD`).
- **Group** = the decision call's pick, overridden first-rule-wins in
  `company_context.apply_company_fit_response`: `too-senior`/`wrong-role` →
  passed; `comp-mismatch` → passed; craft `weak` → passed; send-worthy with
  taste `weak` → chat-worthy; send-worthy or wrong-timing with craft `unclear`
  → chat-worthy; send-worthy without `strong-fit`/`adjacent-fit` → chat-worthy;
  send-worthy with move ≠ `plausible` → chat-worthy, **except** an `unclear`
  move when the plan has no `comp_band` (missing input, not evidence); rows the
  gate demoted carry `held_by_move_gate` and the summary counts them. A human
  `fit_override` wins outright.
- **Order inside a group** = `rerank_score` desc. The panel never reorders.
- Labels are the enums in `fit_contract.py`: role fit
  `strong-fit | adjacent-fit | promising-step-up | junior-could-grow | too-senior | wrong-role | unclear`;
  move `plausible | comp-stretch | comp-mismatch | wrong-timing | destination-pull | founder-lock-in | unclear`;
  taste and craft `strong | neutral | weak | unclear`.

## `epoch0/plan.json`

Produced by `build_eval_inputs.plan_from_obj` from the model's raw JSON.

| Field | Source |
| --- | --- |
| `job_title`, `normalized_archetype`, `pond_prompt_family`, `hire_stage`, `target_level` (default `senior_ic`), `usable_cutoff` | model; family off the enum → `general` |
| `hiring_company{name, website_url}` | model name, else `source.json` |
| `candidate_populations[{population, hint_kind, evidence_quote}]` | model; ten hint kinds; quote must be a verbatim JD substring; max 12 |
| `comp_band` | model; verbatim quote or `null` |
| `search_scope{location, filters}` | model location → `location_scope.canonicalize_generated_location_filters`; `null` = global |
| `traits.must_have[{trait, tier: core, source}]` | model; capped at `MAX_CORE_TRAITS` = 4; non-core traits are demoted to `filters` or `nice_to_have` |
| `traits.nice_to_have[]` | model |
| `core_groups[]` | `plan_filters.compile_core_groups`: every two-thirds combination of the core traits. No consumer since the exhaustive engine was deleted; the flat trait contract (`docs/trait-extraction-redesign.md`) replaces `must_have` / `nice_to_have` / `core_groups`. |
| `filters[]`, `retrieval_filters` | model filters + `"Based in <location>"`; years-of-experience compiled by `bind_plan_filters` |
| `recruiter_policy` | `recruiter_policy.resolve_recruiter_preferences(user > jd > policies/recruiter-defaults.json)`; the model's own `recruiter_preferences` output is ignored |

## Precedent cards (`precedents.py`)

Three card kinds, all ranked by TF-IDF overlap of `{title, occupation}` and
`defining_capability` against the card's `job/family` and
`defining_capability`, with score floors and an `excludes` veto:

- **move cards** — 50 seeds in `packs/search/policies/search-harness-precedents.json`
  plus any results.json iteration with `proposal_delta.reviewed: true`.
  Consumed by Pond-1 generation (top 1, recorded in `queries.raw.json`) and
  `decide` (top 3, recorded as `next_move_precedents`).
- **payload-edit cards** — iterations with `human_edit_delta` or
  `payload_reviewed`. Consumed by the terra pattern-defaults call.
- **fit cards** — seed `fit_cards` (currently empty) plus
  `shortlist_grades[].fit_override.reviewed`. Consumed by the panel.

Nothing in this repo writes `proposal_delta.reviewed` or `fit_override`; a
stock install retrieves seed move cards only. `user-edits.jsonl` (from
`search_feedback.py log`) is never read by precedents.

## Cost accounting

The shared OpenAI client appends one row per call to
`POWERPACKS_USAGE_LOG` (= `<run>/usage.jsonl`) with `stage`; `_price_usage_log`
fills `cost_usd` from `packs/search/data/model-prices.json`.
`iteration.cost_usd` sums rows tagged `pond_NN`, which excludes the
expansion/filter/rerank subprocess rows (they carry the pipeline's own stage
tags); `manifest.cost_usd` and `summary.total_cost_usd` sum everything.
RapidAPI company lookups are tracked separately in `results.json.rapidapi`.

## Files

| File | Role | Reads | Writes |
| --- | --- | --- | --- |
| `deep_search_loop.py` | CLI door: JD intake, plan validation, corpus identity, plan binding, hand-off to the harness | `decision.json`, `jd.txt`/URL, `epoch0/plan.json`, `queries.json`, `--preferences` | `jd.txt`, `source.json`, canonical `epoch0/plan.json`, `plan_binding.json` |
| `search_harness.py` | The engine: `set-query`, `compile-pond`, `review-payload`, `run-pond`, `decide`, `reannotate-saved`; results/manifest/summary/CSV export | run dir artifacts, `usage.jsonl`, `PATTERN_DEFAULT_PROMPT`, family `next-pond` prompt, move/payload-edit/fit cards | `results.json`, `manifest.json`, `ponds/pond-NN/*`, `shortlist.csv`, `relationship.csv`, `network_floors.json` |
| `company_context.py` | Hiring-company and candidate-company context (TurboPuffer lookup, RapidAPI cache-first); the five panel prompts; deterministic group override and the move gate | RapidAPI cache dir, `RAPIDAPI` key | cache files |
| `fit_contract.py` | Enums for panel dimensions/labels/groups; `FitCard` parser | — | — |
| `precedents.py` | Card retrieval (move, payload-edit, fit) from the seed policy file and reviewed `results.json` history | `policies/search-harness-precedents.json`, `.powerpacks/deep-search/*/results.json`, `$POWERPACKS_SEARCH_HARNESS_LAB_ROOT` | — |
| `build_eval_inputs.py` | JD → plan (one model call) | JD, `source.json`, `trait_generation.txt`, recruiter defaults | `epoch0/plan.raw.json`, `epoch0/plan.json` |
| `decompose_jd.py` | JD + plan → the Pond-1 query | JD, plan, family `pond-1` prompt, one move card | `queries.raw.json`, `queries.json` |
| `pond_prompts.py` | Resolve `pond-1` / `next-pond` prompt by `pond_prompt_family` | `packs/search/prompts/**` | — |
| `network_floors.py` | Exact-token population counts per candidate population × plan location | plan, corpus identity | (harness writes `network_floors.json`) |
| `fetch_jd.py` | URL → JD text + source metadata; stdlib HTTP | URL | `jd.txt`, `source.json` |
| `plan_filters.py` | English filter normalization, YOE retrieval filters, two-thirds core groups, payload filter enforcement | plan | — |
| `location_scope.py` | Location vocabulary, plan scope validation, payload geo enforcement | `packs/indexing/lib/location_normalization` data | — |
| `recruiter_policy.py` | Load/validate/resolve recruiter defaults with provenance; render the policy prompt block | `policies/recruiter-defaults.json` | — |
| `results_web/` | Stdlib HTTP viewer over `results.json`; per-candidate feedback POSTs to Powerset | `results.json`, pond artifacts, `jd.txt` | none locally |
| `subprocess_utils.py` | Checked child execution with artifact verification | — | — |

## Run-dir artifacts

`decision.json`, `jd.txt`, `source.json`, `epoch0/plan.raw.json`,
`epoch0/plan.json`, `network_floors.json`, `queries.raw.json`, `queries.json`,
`plan_binding.json`, `results.json`, `manifest.json`, `usage.jsonl`,
`ponds/pond-NN/{prepare/, payload.json, pattern-defaults.raw.json, compile.log, run.log, company-fit/}`,
`user-edits.jsonl`, `feedback-sent.jsonl`, `shortlist.csv`, `relationship.csv`.
Pipeline-side artifacts live under `.powerpacks/runs/artifacts/<task_id>/`.

## Known drift between docs and code (2026-09-02)

- `deep-mode.md` says `mode: auto` in `decision.json` runs unattended —
  nothing reads that key; auto is `decide --autonomous`. It also refers to
  "Marimo" as the review surface; the in-repo surface is `results_web`.
- `decompose_jd.py`'s CLI help and `deep-mode.md` once said "one or two
  queries"; the generator rejects anything but exactly one.
