# Reflect + Search v2 — proposal 🧭

**Created:** 2026-07-30

**Changelog:**
- 2026-07-30 — Arthur's review: production triage is a single selected cheapest model
  (bake-off via Reflect), NOT a runtime MOE — MOE voting stays harness-side for GT
  labeling only; OpenRouter key approved for the `.env` contract; one-engine/profiles
  direction approved; folded in the prior `$reflect` work (Codex session 2026-07-27 →
  draft PR powerset-co/powerpacks#356, `packs/observability/`) — see §3.0.
- 2026-07-30 — initial proposal (repo audit + pathfinder portability study + eval inventory).

---

## TL;DR

- **One engine.** Fold fast search into the deep-search loop. "Fast" becomes the deep
  engine run with 1 seed, 1 round, and an exit after the cheap-rank layer. "Deep" is the
  same engine allowed to continue into judge → consensus → expand epochs. Every query is
  a *profile* (`lookup | fast | gtm | recruit`) that sets seed count, exit layer, and
  overlays — not a separate pipeline.
- **Reflect is the harness, and it comes first.** Nothing about the engine should change
  until we can measure it: real per-call token/cost capture, per-stage wall-clock, a
  ground-truth **survival funnel** (which stage lost which GT person), per-probe recall
  attribution, and cross-JD aggregation with regression gates. ~70% of Reflect already
  exists as unwired pieces (`score_ground_truth_gaps`, `judge_consensus`, `codex_judge`,
  plan binding, the decision eval's report shape). Reflect is a **family, not one tool**:
  the in-flight `$reflect` telemetry skill (draft PR #356) is the user-side half and
  supplies the canonical node timing contract; the JD bench is the maintainer-side half
  and consumes it (§3.0).
- **10× the filtering cost down, keep the rerank/judge cost.** Add OpenRouter as a
  provider (one `base_url` swap — everything already goes through the OpenAI SDK), then
  run a Reflect bake-off across cheap open models (Kimi / DeepSeek / GLM / current
  gpt-4.1-mini) and ship **the single cheapest model that preserves the most recall**
  as the one production triage model. No runtime MOE for filtering — multi-model voting
  is a harness-side tool for GT labeling only. Separately, stop re-running the
  8-extractor expansion fan-out per probe (~250+ LLM calls per deep run today) by
  caching expansion per unique seed.
- **Kill the company/directory surface, keep its resolvers.** `search-company` as a
  user-facing skill retires; its ~1,700 LOC of resolver primitives stay as the engine's
  internal entity-resolution layer. "Who works at X" and other non-network questions
  route to agentic SQL over the local DuckDB (`search-sql`), whose schema cheat sheet
  becomes generated, not hand-maintained.
- **Pathfinder comes back as an overlay, locally.** The tenure-overlap graph from
  `network-search-api` ports to DuckDB as a self-join — our local data already has 3,909
  dated positions with stable `company_key`s across all 500 people. Dossier signals
  (message counts, recency, channel mix, shared context) give us *measured* owner→contact
  edge strength the original never had. GTM searches rank by `fit × intro_strength`.

---

## 1. Where we are today (audit findings)

Four deep dives (search pack, eval infra, `../network-search-api` pathfinder, dossier/
sales-nav/people-store surfaces) produced the following load-bearing facts.

### 1.1 Two pipelines that are really one, already drifting

| Concern | Fast path | Deep path | Status |
|---|---|---|---|
| Cheap filter | `llm_filter_candidates.py` (gpt-4.1-mini, batch 2, conc 1000, task-state coupled) | `triage_candidates.py` (gpt-4.1-mini, batch 15, conc 8, frontier-file coupled) | duplicated; triage's own docstring says it exists only because the filter is welded to task-state |
| Ranking | `llm_rerank_candidates.py` (gpt-5.1, 0–1 scores) | `evaluate_profile_candidates.py` (gpt-5.4, per-trait statuses + deterministic scorer) | incompatible contracts; deep disables the first with `--filter-only` |
| Score ladder | `evaluate_profile_candidates.py:63` | `judge_consensus.py:39` | **already drifted** (`foundational` 0.50 vs 0.45, `thin` 0.25 vs 0.30) |
| RRF | `search_common.py:921` | `local_duckdb_store.py:827` + `merge_ranked_rows` | three implementations, `K_RRF=60` declared twice |
| Recruiter policy | prose in SKILL.md:178–224 the agent must remember | versioned `policies/recruiter-defaults.json` + resolver | two representations of one policy; founder exclusion restated a third time in search-sql |
| Location | `search_common.py:524,538` | `location_scope.py` (745 LOC) | duplicated contract |

The deep loop also re-enters the *entire* fast `prepare` expansion fan-out once per probe:
16 seeds × 2 rounds × 8 extractor calls ≈ **250+ expansion calls before a single judge
call**, with per-probe failures silently swallowed.

### 1.2 The skill leans on agent improvisation where primitives should exist

- Person lookup is hand-written SQL in the SKILL against a hardcoded table name — it
  silently misses when the index used the alternate accepted name
  (`local_people_profiles` vs `local_person_profiles`).
- `search-sql`'s schema cheat sheet is a hand-maintained copy of the real DuckDB schema.
- Filters on missing columns compile to constant clauses (no-match, or match-all for
  negations) with only a stderr warning — a documented real parity failure
  (`local_duckdb_store.py:78–97`).
- Seniority/hireability policy on the fast path is prose the agent must apply from
  memory; deep mode gets the same policy as versioned JSON.

### 1.3 Everything is OpenAI; nothing is priced

- No OpenRouter, no non-OpenAI provider anywhere. All calls go through
  `shared/openai_client.py`. (The only alternative inference path is the `codex exec`
  subprocess judge.)
- Models in play: expansion gpt-4.1 ×7 + gpt-5.4 (company); filter gpt-4.1-mini; rerank
  gpt-5.1; plan gpt-4o; critic gpt-5.4; judge gpt-5.4; indexing gpt-5.1/5.2.
- **Cost is not measured.** `--cost-usd` in `convergence.csv` is a manual flag that is
  `0.0` in every run on disk. Token accounting is a prompt-side tiktoken estimate at
  exactly two call sites. No completion tokens, no USD, no per-stage wall-clock in the
  deep loop. There is no spend cap or budget abort anywhere in either path.

### 1.4 Eval infra: strong pieces, no system

Exists and is good: the Step-1 decision eval (rules extracted verbatim from the SKILL,
strict/lenient scoring, confusion matrices, CI-locked coverage floors);
`score_ground_truth_gaps.py` (recall@k / precision@k / missed / net_new → idempotent
`convergence.csv` rows); `judge_consensus.py` (multi-judge fan-in that preserves
dissent, mixed-schema tolerant); `codex_judge.py` (a free judge byte-identical in rubric
to the paid one); plan/corpus binding via SHA-256 (`plan_binding.json`) so runs are only
comparable when plan + JD + corpus match; the anti-local-maxima methodology written down
in `deep-mode.md:374–393`.

Missing: committed JD suite (all JD text is gitignored; recall fixtures live in an
external repo that isn't present); stage-wise GT survival funnel (hand-written once in a
REPORT.md, never computed); per-probe recall attribution (the frontier schema already
captures `matched_probe_ids` + `source_rows`, nothing joins them to GT); ordering metric
(NDCG — `ordering_eval.csv` re-runs rank-insensitive recall@k); cross-JD aggregation and
regression gating; judge-panel fan-*out* (fan-in exists); real cost/latency capture.

### 1.5 Pathfinder is portable and the data is ready

`network-search-api` has a live Neo4j pathfinder: tenure-overlap edges weighted by a
headcount × overlap-months lookup table (small company + long overlap → 0.95; ≥1000
headcount effectively filtered out by the 0.30 weight floor), education edges, Sales-Nav
`EXPLICIT_LINK` edges that deliberately outrank inferred ones, and additive
inverse-weight shortest-path ranking. The scoring core (`_compute_weight`,
`_overlap_months`) is pure Python, zero Neo4j dependency.

Locally: `people.csv` `work_experiences` is 100% populated — 3,909 positions, 99.8%
dated, 1,943 distinct `company_key`s (one sentinel trap: `rapidapi:0` appears 452× and
must be excluded or it forms a false clique). `owner.json` has the owner's own dated
timeline. Dossiers (288/500 people) carry `message_count`, `last_interaction`, channel
mix, `confidence`, and 78 typed shared-context overlaps. Sales-nav leads carry
`mutual_count` / `mutual_member_ids` when a live LinkedIn session exists. What does NOT
exist locally in any form: a 1st-degree LinkedIn edge list. Risk to track: local company
`headcount` coverage may be sparse; the weight table needs it (fallback below).

### 1.6 Dead weight

The 19-step V1 task lifecycle (`search-network.task.json`) with its ~10 unused schemas
and orphan primitives (`execute_search_slice`, `merge_candidate_frontier` legacy pieces,
`agentic_candidate_review`, `count_candidates`), the investor index builder (now owned by `packs/indexing/primitives/build_investor_index/`),
consumers), the deleted router's committed `.pyc`, `export_candidate_shortlist.py`, the
`ground_truth_ranked.json` compatibility alias, and an env merge that reads
`../network-search-api/.env` from inside `search_network_pipeline.py:113`. The orphaned
`search-network/cases.json` rubric eval no longer resolves after the skill rename.

---

## 2. Target architecture: one engine, layered, with early exits 🏗️

### 2.1 The layer stack

```
L0  Intake & Plan      query/JD → typed SearchSpec (traits, filters, location scope,
                       seniority policy, expected-pool-size class). ONE policy home
                       (recruiter-defaults.json) for every profile.
L1  Source             seed generation (1..N) → cached expansion → entity resolution
                       (company/education resolvers — the former search-company guts)
                       → hybrid retrieval (ONE RRF) → union frontier
L2  Triage             ONE cheap-filter primitive (merges llm_filter + triage_candidates).
                       Provider-agnostic; MOE-able across cheap open models.
L3  Rank               banding + rerank. ← FAST EXITS HERE
L4  Judge              evidence judge + consensus + core/location gates (deep only)
L5  Expand             expand-from-anchor epochs until convergence (deep only)
L6  Overlays           post-hoc annotators that reorder/annotate a result set:
                       intro-graph (pathfinder), sales-nav supplement, dossier context
```

### 2.2 Profiles replace the fast/deep fork

| Profile | Trigger | Configuration |
|---|---|---|
| `lookup` | bare name/email/phone/handle/URL | deterministic `lookup_person` through the selected backend |
| `gtm` | "find people by role, level, or company archetype" | structured filters, bounded retrieval, hydration, and rank, then exit after L3 |
| `recruiting` | JD / posting URL / shortlist ask | full L0–L5, human plan gate at L0, epochs until convergence |

The Step-1 decision uses `SearchRoute(target, profile, backend, reason)`. Engine
routes select `lookup`, `gtm`, or `recruiting`; `sql` remains the escape hatch for
relational/aggregate questions and `contacts` remains an explicit non-engine target.

Sizing discipline (your 1c): L0 records an expected-pool-size class (from the pool
estimate + the plan). Sparse-family searches (`expected: handful`) get tightened caps —
fewer seeds, smaller top-k, earlier epoch stop — and a per-run `--max-usd` budget abort
(new; today no spend cap exists anywhere). The engine should *converge to few* cheaply,
not fan out identically for every job family.

### 2.3 Where agent taste lives (and where it doesn't)

The agent keeps: intent → profile decision, plan presentation and the single human
review gate, group-source judgments, the one clarifying question, and final
presentation/synthesis. Everything else that is currently prose-the-agent-must-remember
becomes code: person lookup, directive stripping, seniority policy application, schema
knowledge (generated cheat sheet via `local_duckdb_query.py schema --markdown`), missing-
column handling (hard error with a suggestion, not a stderr warning that silently
degrades filters).

---

## 3. Reflect — the hill-climbing harness 🔬 (priority 1)

### 3.0 Fold-in: the existing `$reflect` work (draft PR #356)

A 2026-07-27 Codex session already built a thing named `$reflect` — **not** a search
benchmark, but a privacy-safe **workflow telemetry + self-review reporting** skill:
`packs/observability/` (skill, CLI primitive, closed-enum fail-closed export contracts,
664-line test suite), shipped as draft PR **powerset-co/powerpacks#356** (open,
unmerged, 34 files / +2,777 lines). It also landed the piece we need most: a
**canonical node timing contract** — every converted pipeline `Node` writes top-level
`timing: {started_at, finished_at, duration_seconds}` on success, blocked, and failure
(implemented for `$setup`, `$import-gmail`, `$import-messages`, `$deep-context` on that
branch, including per-phase timings for the deep-context enrichment chain).

Resolution — **one Reflect family, two halves**:

- **User-side half — `$reflect` (PR #356), unchanged.** Per-workflow retrospectives,
  anonymized closed-schema export, Powerset auto-upload / GitHub-issue preview routing.
  Its north-star metric ("validated workflow completion without avoidable
  intervention", with agent-claimed vs artifact-validated success tracked separately)
  stays the operational metric; the JD bench is a sibling evaluation layer, not a
  replacement.
- **Maintainer-side half — the JD bench (this section).** Runs locally against the
  corpus, never uploads, and is where hill-climbing lives — honoring that session's
  hard rule that **user harnesses never write code**: any loop that edits prompts,
  policies, or skills is maintainer-side and human-gated.

What the bench adopts from #356 rather than reinventing:

1. **The timing contract.** Search stages and deep-loop epochs emit the same top-level
   `timing` block shape (same field names) in their ledgers/`loop.json` — no recursive
   `elapsed_ms` scraping (an explicitly corrected bug in that session), no second
   timing scheme. Phase 0's "per-stage wall-clock" item becomes "extend the #356
   contract to the search pipeline".
2. **Model × effort as first-class dimensions.** Every metric slice (success, latency,
   cost, funnel survival) is keyed by model and reasoning effort, so "Powerpacks
   changed" and "the model changed" are separable.
3. **The privacy discipline.** Bench artifacts follow the same posture: GT sets and
   per-person rows stay local; anything that ever leaves the machine goes through a
   closed-schema projection.

Sequencing: review + land PR #356 (at minimum its timing-contract half) as part of
Phase 0, then build the bench on top of it.

### 3.05 Layout

The bench lives at `packs/search/reflect/` (in-repo; extract only if it outgrows this
repo — it needs the corpus and primitives next to it). The `packs/observability/` pack
from #356 stays the home of the user-side skill.

### 3.1 The benchmark suite

```
packs/search/reflect/suite/<jd-slug>/
  jd.txt              # committed — portfolio-company postings are public text
  meta.json           # job_family, expected_size_class (handful|dozens|hundreds),
                      # source URL, date, corpus fingerprint it was labeled against
.powerpacks/reflect/gt/<jd-slug>/gt.json    # ground truth: person_ids + tiers
```

GT person-ID sets stay **local, gitignored**, keyed to a corpus fingerprint (the
existing `resolve_retrieval_identity` binding), per the repo's contact-privacy rules —
committed artifacts carry only counts and hashes. The suite must span job families:
eng IC, infra/deep-tech, GTM/sales, product/design, exec, and at least one deliberately
sparse niche family. Per the written anti-local-maxima rule, every engine change is
evaluated on ≥2 structurally different JDs — Reflect enforces this in code instead of
leaving it to discipline.

**GT labeling** is a one-time-per-JD deliberate act: MOE judge panel fan-out
(gpt-5.4 + codex + 2–3 open models via OpenRouter) → existing `judge_consensus.py` with
`--min-inband-votes 2 --min-notout-votes 2` → human skim of dissents. Expensive is fine
here; labels amortize across every future run. This is the same fan-out infra as
production triage, run at a higher rigor setting — build it once
(`judge_panel.py`: spawn N judges writing `judges/<name>.jsonl`; consensus already
exists).

### 3.2 Instrumentation (build first — nothing hill-climbs without it)

1. **Usage capture at the client.** One wrapper in `shared/openai_client.py` records
   `{model, prompt_tokens, completion_tokens, reasoning_tokens, latency_ms, stage_tag}`
   for every call, appended to the run dir. A committed `model-prices.json` turns usage
   into USD. `cost_usd` and `elapsed_s` flow into `convergence.csv` automatically —
   the manual flag dies.
2. **Per-stage wall-clock** in `loop.json` epoch rows and stage ledgers, using the
   PR #356 node timing contract (`timing: {started_at, finished_at, duration_seconds}`,
   top-level, on every terminal outcome — success, blocked, failed). The fast path's
   ad-hoc `elapsed_ms` events migrate to the same shape; deep has none today.
3. **GT survival funnel primitive** (`score_funnel.py`): joins `union.jsonl` →
   `candidate_frontier.full.jsonl` → `triage.json` → `judges/loop.jsonl` →
   `consensus.json` against GT and emits the table the AgentMail REPORT.md wrote by
   hand: `31 GT → 30 sourced → 29 triaged → 29 judged → 5 shortlisted`, with a
   disposition histogram for every loss (never-sourced / triage-dropped / core-gated /
   seniority-gated / below-floor / never-judged). This converts one recall number into
   per-stage attribution — the single highest-value new piece.
4. **Per-probe attribution**: join `matched_probe_ids` / `source_rows` (already in the
   frontier schema) against GT → per-probe-family yield and marginal value, to tune
   seed count and probe diversity with data.
5. **Ordering metric**: NDCG@k / rank correlation vs GT tiers (recall@k is
   rank-insensitive and showed identical numbers before/after micro-sort).

### 3.3 The runner and the gate

```
reflect run   --suite all|--jd <slug> --config <name|grid>   # engine → score → funnel
reflect report                                               # cross-JD aggregation
reflect gate  --baseline <report>                            # regression check
```

- A **config** is a named bundle of the knobs that already exist as CLI flags
  (`--score-threshold`, `--judge`, `--n` seeds, `--anchors`, `--max-epochs`,
  `--reasoning-effort`, triage on/off, model choices per layer, policy weights). The
  hill-climb sweeps configs, never edits per-JD anything.
- `report` aggregates mean / min / variance per metric **per job family**, plus total
  cost and wall-clock.
- `gate` fails a change that improves one JD while regressing another beyond epsilon,
  and enforces floor checks (`--min-recall`, `--max-cost`) — the decision eval's
  two-consecutive-runs floor policy, generalized. Committed baseline reports get a CI
  freshness check (the decision eval's baseline is already stale at 68/70 cases —
  don't repeat that).

Traps encoded, not remembered: never score against `shortlist/ground_truth_ranked.json`
(it's the run's own output, not GT); comparisons invalid across corpus fingerprints;
judge errors excluded, never counted as rejections.

---

## 4. Cost: 10× down on filtering, hold the line on judging 💸

Where the money goes today: gpt-5.1 rerank (conc 400) and gpt-5.4 judge dominate;
expansion fan-out (8 models/probe, re-run per probe) is pure duplication; the filter is
already on a mini model but is one of two duplicated stages.

1. **OpenRouter provider** — `make_openai_client` grows a provider param
   (`OPENROUTER_API_KEY` + `base_url`); every primitive inherits it via the one shared
   client. No per-primitive changes.
2. **Triage model bake-off at L2** — Reflect runs the unified triage primitive over
   the JD suite once per candidate model (Kimi K2, DeepSeek, GLM, gpt-4.1-mini
   baseline) and compares GT funnel survival vs cost. Production ships **one model**:
   the cheapest whose triage-stage GT survival is non-regressed vs baseline. No
   runtime MOE here — one model, one prompt, conservative keep-threshold; the
   recall-protection comes from the threshold and the funnel gate, not from voting.
   Re-run the bake-off occasionally as new cheap models land (it's just a Reflect
   config sweep). A cross-encoder stays off the table for nuance reasons (agreed);
   a cheap LLM with a conservative threshold is the middle ground. Multi-model
   fan-out survives only in the harness (GT labeling panel, §3.1).
3. **Expansion caching** — expansion output cached by seed-text hash; the deep loop's
   ~250 expansion calls collapse to ~1 per unique seed. Also dedupe seeds across
   rounds before probing.
4. **Budget ceilings** — per-run `--max-usd` + per-stage token caps, surfaced in the
   run manifest. Sparse-profile runs get small defaults (1c).
5. **Judge stays premium** — gpt-5.4/codex for final evidence judging where nuance
   pays; consensus thresholds `(2,1)`+ once panels are cheap enough to be routine.

---

## 5. Surface cleanup 🧹 (priority 3)

| Surface / code | Action |
|---|---|
| `search-company` skill | **Retire the user surface.** Resolvers (~1,700 LOC) move to an internal `primitives/resolve/` home — they are L1 of the engine, load-bearing for every company-filtered search. |
| "Who works at X" / directory asks | Route to agentic SQL (`search-sql`) over the local DuckDB; the fast path's company-directory detection folds into the `fast` profile. |
| `search-sql` | **Keep** as the escape hatch + fan-out lane. Cheat sheet becomes generated from the live index; agent runs `schema` first, always. |
| V1 lifecycle: `search-network.task.json`, ~10 schemas, `execute_search_slice`, legacy `merge_candidate_frontier` pieces, `agentic_candidate_review`, `count_candidates` | Delete (verify real consumers by grep at execution time; `capture_jd_evaluations` is live and stays). |
| `route_query` `.pyc`, `export_candidate_shortlist.py`, `ground_truth_ranked.json` alias | Delete after their canonical consumers are proven. The investor index builder remains owned by `packs/indexing/primitives/build_investor_index/`. |
| `../network-search-api/.env` merge in `search_network_pipeline.py:113` | Delete — sibling-repo path baked into the env loader. |
| Orphaned `search-network/cases.json` rubric eval | Re-point at the renamed `search` skill; it becomes the plan-quality half of the JD benchmark. |
| Duplicated constants/stages (STATUS_VALUE, RRF ×3, filter ×2, location ×2, policy ×2) | Single homes, per the one-home-per-concept rule. |

---

## 6. Overlays: warm intros and the graph 🕸️

### 6.1 Local intro graph (port of pathfinder)

- `build_intro_graph.py`: DuckDB self-join on `company_key` over dated positions
  (exclude `rapidapi:0`), lift `_overlap_months` + `_compute_weight` verbatim, education
  edges second-class, best-edge-per-pair, weight floor 0.30. Edges land in the search
  DuckDB (`local_intro_edges`). Owner edges from `owner.json` — **upgraded by dossier
  evidence**: measured `message_count` × recency × channel mix × `shared_context`
  replaces inferred weight for the 288 people who have dossiers. That's a signal the
  original pathfinder never had for anyone.
- Headcount risk: where local `company_headcount` is empty, fall back to a coarse
  bucket from `network_companies.csv` `contact_count` or treat as unknown-small rather
  than unknown-huge (the original's unknown→10k default would erase edges here).
- L6 overlay primitive: annotate any result set with best path (owner is the only
  operator locally, so paths are ≤2 hops in practice) + `intro_strength`; `gtm` profile
  sorts by `fit × intro_strength`.

### 6.2 Sales-nav supplement (later, gated on a configured account)

`mutual_count` / `mutual_member_ids` from lead search + `enrich_mutual_attribution`
become `EXPLICIT_LINK`-style edges (sub-1.0 cost, outranking inferred — same trick as
the original). Requires Auth0 + MCP + live Unipile session; strictly additive to the
local graph, never a dependency. The powerset-wide graph is the long-run version of
this. `import-sales-nav-leads` (promotion into local contacts) remains named future
work and is a prerequisite for leads participating as path endpoints.

---

## 7. Phasing 🗺️

Every phase merges only through `reflect gate`.

- **Phase 0 — instrument** (no engine changes): review + land PR #356 (at minimum the
  node timing contract), extend that contract to search stages, usage capture + price
  table, `score_funnel.py`, per-probe attribution, NDCG. Reflect bench runner MVP
  wrapping the existing scorer. Commit 3–5 portfolio JDs across ≥3 job families; build
  GT via the judge panel + consensus + human skim.
- **Phase 1 — baseline**: run the suite on the current engine as-is; commit the
  baseline report; set floors. This is the "before" photo — nothing is allowed to be
  slower/blinder than this again.
- **Phase 2 — cost**: OpenRouter provider, expansion caching, unified L2 triage
  primitive, triage model bake-off → ship the single winner. Target: ≥10× filter-layer
  cost reduction at non-regressed funnel survival; judge spend unchanged.
- **Phase 3 — unify**: profiles + early exits (fast folds into the engine), Step-1
  decision simplification, `lookup_person` primitive, policy single-home, dead-code
  deletion, `search-company` surface retirement, generated SQL cheat sheet.
- **Phase 4 — overlays**: intro graph + GTM ranking; sales-nav supplement when a
  configured account exists.

---

## 8. Open questions for Arthur ❓

Resolved 2026-07-30:
- ~~OpenRouter key~~ — **approved**; add `OPENROUTER_API_KEY` to the `.env` contract
  and doctor checks.
- ~~Runtime MOE for filtering~~ — **rejected**; single cheapest recall-preserving
  model, selected by Reflect bake-off. MOE remains harness-side (GT labeling).

Still open:
1. **GT storage**: local-only gitignored GT sets (recommended, per the contact-privacy
   rules) means Reflect baselines are only reproducible on machines with the corpus.
   Acceptable, or do we want an encrypted/committed variant?
2. **Backend scope for hill-climbing**: recommend local-DuckDB-first (deterministic,
   free, fast; the local/prod parity harness already bridges to powerset) — powerset
   parity re-checked at phase boundaries only. OK?
3. **Judge panel for GT**: comfortable spending real gpt-5.4 + open-model tokens on
   one-time GT labeling per JD (~hundreds of judged profiles per JD)?
