---
name: recruit
description: Emulate a recruiting team end-to-end for a JD against a Powerset set — shotgun many small archetype searches, judge the pool with a mixture-of-judges (recruiter/talent-analyst/manager) on the canonical rubric, measure which profiles yield good pools, expand the best via expand-from-anchor, and track convergence toward a judged ground-truth set over epochs. Use for "$recruit", "find/rank candidates for this JD", "build a shortlist from my network", "who in my set fits this role". Supersedes the deleted search-highlight harness.
---

<!--
Created: 2026-06-26
Changelog:
- 2026-06-26: Initial skill. Replaces search-highlight. Built on the empirical finding that
  the existing search_network_pipeline has excellent recall (a single loose probe contains
  100% of ground truth at depth) but noisy single-query ranking — so the lever is SHOTGUN
  (many diverse probes) + a mixture-of-judges, not a new backend. See
  packs/search/docs/agentic-search.md and recruit-ground-truth-status.md.
-->

# recruit

Use for `$recruit`: source, judge, and rank candidates for a JD from a Powerset set, the way a
recruiting team would. This is the productized version of the agentic-search method in
`packs/search/docs/agentic-search.md`.

## The core finding this skill is built on (read once)

The existing `search_network_pipeline` is **not** recall-limited. A single broad probe at full
depth contained **31/31** ground-truth people — but a single query's ranking scatters them
(rank 5 → 5089), so a top-50 cap keeps only ~16%. **Diverse probes fix this:** a probe tailored
to "schedulers" top-ranks the scheduler people, an "inference" probe top-ranks the inference
people, etc. Measured convergence (recall vs a 31-person judged ground truth):

| sourcing | pool | GT recall |
| --- | --- | --- |
| 1 naive probe, keep top-50 | 50 | 16% |
| shotgun (~18 probes), keep top-40 | 509 | 65% |
| shotgun, keep top-80 | 954 | 100% |

So: **shotgun for recall, judge for precision.** Don't tighten retrieval to get precision —
that's what drops good candidates. Keep recall high and let the judges gate.

## Flow (every step is a callable primitive — Codex / any harness runs it identically)

Legend: 🆕 = new `recruit/` primitive · ✅ = existing primitive. No step relies on a harness
improvising; the only LLM calls are `decompose_jd` (1 call) + the judge.

1. **Decompose the JD → diverse seeds** 🆕 (1 LLM call). Emits ~18 diverse, *work-described*
   seeds (what the person built, not titles) — diversity here is what drives recall.
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/recruit/decompose_jd.py \
     --jd-file <run>/jd.txt --n 18 --out <run>/seeds.json
   ```

2. **Shotgun source** 🆕 runner over ✅ primitives (read-only, ~free). One command chains
   `prepare --preserve-query-semantic` (raw query as vector + expansion BM25 + filters:
   location/education/company/seniority/headcount) → `diversify_probe_bm25` (drop shared lead
   terms) → `run --search-only` (unique `--ledger` per probe) → deduped union with profiles:
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/recruit/run_shotgun.py \
     --seeds <run>/seeds.json --run-dir <run> --limit 200 --top-k 6000
   ```
   Writes `<run>/union.jsonl`. (Validated end-to-end: 12 auto-seeds → 90% recall vs GT; 18 → ~97%.)
   **Why the flags matter:** without `--preserve-query-semantic` expansion rewrites the vector and
   ~halves recall; without diversify the probes overlap. Both are defaults in the runner.

3. **Bridge the union into the judge's contract** 🆕 (1 LLM call). The canonical judge reads a
   profile-search run dir (`plan.json` + `candidate_frontier.jsonl` + `probe_summaries.json` →
   the already-on-disk `profiles.jsonl.gz`). `build_eval_inputs` extracts must/nice traits from
   the JD and rewrites the shotgun run into exactly that contract — no recompute:
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/recruit/build_eval_inputs.py \
     --run-dir <run> --jd-file <run>/jd.txt --set-name "<set>" --created-at <iso>
   ```

4. **Triage (tier-1, only if the union is big > ~120)** 🆕 `triage_candidates` — cheap-model
   (`gpt-4.1-mini`) conservative filter over `candidate_frontier.jsonl` that drops obvious
   non-matches and passes borderline (`keep`/`maybe` survive). It reads the PROFILE, not probe
   count — **measured: probe count is a bad precision signal** (single-probe hits include true
   top candidates), so don't cap by `found_by`. Survivors overwrite `candidate_frontier.jsonl`
   (original saved to `candidate_frontier.full.jsonl`). Small unions skip this.
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/recruit/triage_candidates.py --run-dir <run>
   ```
   *(NOTE: the search-profile `llm_filter_candidates` is welded to a `search_network_pipeline`
   run-state — it does NOT fit the recruit union artifact; use `triage_candidates`.)*

5. **Judge (the precision stage)** ✅ `evaluate_profile_candidates` — the canonical bar-raiser
   rubric with the IC seniority hard-gates (default `gpt-5.4`). **Default to a CROSS-VENDOR panel:**
   one `gpt-5.4` judge at `--reasoning-effort low` (measured: ~as good as `high` here, far cheaper)
   **+ one Claude judge on the same rubric.** Cross-vendor agreement is the real confidence signal —
   when both vendors say strong, surface it; when they split, that's the human-review pile. Collect
   each pass's verdicts into a `judges/` dir; `judge_consensus` ingests the
   `evaluate_profile_candidates` raw format directly (maps `candidate_id`/`jd_score`, derives
   `in_band` from `seniority_fit`) alongside native Claude-judge JSONL.
   - **FREE / portable judge:** `recruit/codex_judge.py` spawns `codex exec` subprocesses, reusing
     the *exact* canonical rubric + deterministic scorer (so the bar is identical, the engine is $0
     via ChatGPT-subscription auth). This is the default cheap judge; the paid `gpt-5.4` API path is
     an optional cross-vendor second opinion. A Claude-CLI variant is the same shape once `claude`
     is installed.
   - **Tune the shortlist cutoff, don't loosen the rubric.** Measured (AgentMail): every strict
     LLM judge (gpt-5.4 *and* codex) rejects ~40–50% of a leniently-built GT at the default
     verdict cutoff (~0.50). Lowering `judge_consensus --score-threshold` to ~**0.40** recovers
     **~0.9 recall while admitting only ~4–6 non-GT of 42** — the gap was calibration, not
     sourcing/vendor. A **cross-vendor union** (codex OR gpt keeps) lifts recall further (~0.96).
     Validate the threshold on a 2nd JD before hardcoding a default.

6. **Consensus + rank** 🆕:
   ```bash
   uv run --project . python packs/search/primitives/recruit/judge_consensus.py \
     --judges-dir <run>/judges --union <run>/union.jsonl --out-dir <run>/shortlist \
     --min-inband-votes 2 --min-notout-votes 2   # single judge: use 1/1
   ```
   → `shortlist/ground_truth_ranked.json` (consensus-strong, stack-ranked). Canonical gate:
   majority in-band AND majority not-out (2/2 of a 3-judge panel). Then ✅
   `export_candidate_shortlist` for the sendable CSV. **Measured (AgentMail JD):** single judge
   → recall 48% / p@25 0.36; 3-judge (2,2) → 52% / 0.44; a `(2,1)` gate (majority in-band +
   ≥1 not-out) Pareto-beats it (64% / 0.48) by rescuing borderline candidates one strict judge
   cut — but per the anti-local-maxima rule it stays NON-default until validated on a 2nd JD.

7. **Expand-from-anchor (optional, closes the last ~10%)** 🆕. Take your *own* judged-strong
   picks as anchors, build "more like this" seeds from their profiles, and re-source (loop to
   step 2). NEVER seed from the eval ground truth — that's looking up the answers.
   ```bash
   uv run --project . python packs/search/primitives/recruit/expand_from_anchor.py \
     --anchors <run>/shortlist/ground_truth_ranked.json --top-k 3 --out <run>/anchor_seeds.json
   uv run --env-file .env --project . python packs/search/primitives/recruit/run_shotgun.py \
     --seeds <run>/anchor_seeds.json --run-dir <run>/anchor --limit 200
   ```

8. **Measure convergence (epochs)** 🆕. Score any run against a trusted ground-truth set:
   ```bash
   uv run --project . python packs/search/primitives/recruit/score_ground_truth_gaps.py \
     --ground-truth <run>/ground_truth/ground_truth_ranked.json \
     --epoch-candidates <epoch>/candidates.json \
     --epoch-dir <epoch> --epoch-label epoch-NN --convergence-csv <run>/convergence.csv
   ```
   `gaps.json` = recall@k / precision@k / missed-GT; `convergence.csv` = one row per epoch.

**Ground truth** (the yardstick for hill-climbing) is built once by running steps 1–5 the
*thorough* way — many hand-diverse seeds + the full judge panel — independently of the cheap
recipe you are scoring (so the recall number isn't circular).

## Avoiding local maxima (be strict)

Hill-climbing the harness is easy to overfit to one JD. Hard rules:

- **Never tune to one JD.** Any change that improves recall/precision must be validated on **≥2
  structurally different JDs** (e.g. distributed-systems infra *and* applied-AI product) before it
  becomes a default. A change that helps one and not the other is a local maximum — reject it.
- **Data-driven, not hardcoded.** No JD-specific term lists, company lists, or thresholds baked
  into code. `diversify_probe_bm25` drops shared terms by *measured* document frequency, so it
  adapts per JD (it dropped "distributed systems engineer" for one JD and "ai product engineer"
  for another with the same code). Keep new heuristics this way.
- **Recall via an independent yardstick.** Score epochs against a ground-truth set built the
  *thorough* way (full agentic + judge), not against the cheap run's own output (that's circular
  and rewards overfitting).
- **Watch the whole curve, not one number.** A change that lifts recall@10 but tanks overall
  recall (or re-admits seniority-gate failures) is regression, not progress. Track recall@k,
  precision@k, gate-error, and cost together in `convergence.csv`.
- **Keep the judge canonical.** Improve sourcing/orchestration; do not weaken the bar-raiser
  rubric or the IC seniority gates to make numbers go up.

## Cost

Sourcing is ~free: retrieval (TurboPuffer) is read-only, hydration is Postgres-only; the only
OpenAI spend there is `decompose_jd` (1 call) + the per-seed `prepare` expansions (cheap `gpt-4o`).
`build_eval_inputs` is 1 cheap call; `triage_candidates` is cheap `gpt-4.1-mini` batches
(~$0.20 over ~1k candidates). **The real spend is the judge:** `evaluate_profile_candidates`
(`gpt-5.4`) ≈ a few cents/candidate → ~$15–25 per pass over a ~600 triaged pool; a 3-judge
mixture is ~3×. Measured AgentMail full-chain run (1034→606→3-judge panel) ≈ **~$47**. Triage
HARD before judging to control cost. *(Claude-Code-only sessions can swap the OpenAI judge for
2–3 Claude sub-agents on the same rubric — Claude-priced, ~zero OpenAI, but not portable.)*

## Artifacts (gitignored under `.powerpacks/recruit/<jd-slug>/`)

`BRIEF.md` · `probes/<family>/…` · `candidates_union.jsonl` · `judges/*.jsonl` ·
`shortlist/{consensus.json,ground_truth_ranked.json}` · `epochs/<epoch>/{config,candidates,gaps}.json` ·
`convergence.csv`. Candidate PII stays gitignored; surface the shortlist to the user.

## Default recipe (fully primitive-driven; validated ~90% from auto-seeds, ~97% tuned)

`decompose_jd` → `run_shotgun` (prepare --preserve-query-semantic → diversify_probe_bm25 →
run --search-only) → `build_eval_inputs` → [`triage_candidates` if big] →
`evaluate_profile_candidates` (×N for a panel) → `judge_consensus` →
`export_candidate_shortlist`; optional `expand_from_anchor` loop; `score_ground_truth_gaps` to
track epochs. Every step is a CLI a harness can call — nothing depends on an agent improvising.
Validated end-to-end on the AgentMail JD (1034 sourced → 606 triaged → 3-judge gpt-5.4 panel →
62-person shortlist), zero sub-agents.

## Primitives

New (`packs/search/primitives/recruit/`):
- `decompose_jd.py` 🆕 — JD → N diverse work-described seeds (1 LLM call).
- `run_shotgun.py` 🆕 — runs the seed set through prepare→diversify→run, emits the union.
- `diversify_probe_bm25.py` 🆕 — drop shared/homogeneous BM25 lead terms across the probe set.
- `build_eval_inputs.py` 🆕 — union → `plan.json` + `candidate_frontier.jsonl` +
  `probe_summaries.json` (bridges the shotgun run into the canonical judge's contract; 1 LLM call).
- `triage_candidates.py` 🆕 — cheap-model conservative tier-1 filter over the frontier.
- `codex_judge.py` 🆕 — **free, portable** judge: spawns `codex exec` subprocesses, reusing the
  canonical rubric + deterministic scorer from `evaluate_profile_candidates` (same bar, $0 engine
  via ChatGPT-subscription auth). Drop-in for the paid gpt-5.4 judge; same raw output shape.
- `expand_from_anchor.py` 🆕 — judged-strong anchors → "more like this" seeds (no LLM).
- `judge_consensus.py` 🆕 — combine judge passes (native, `evaluate_profile_candidates` raw, or
  `codex_judge` raw) → consensus shortlist. `--score-threshold` tunes the shortlist cutoff on the
  canonical score (recall/precision dial; ~0.40 recovered ~0.9 recall on AgentMail).
- `score_ground_truth_gaps.py` 🆕 — epoch scoring + convergence vs a ground-truth set.

Existing (reused):
- `search_network_pipeline … prepare --preserve-query-semantic` — keeps the raw query as the
  semantic vector + adds BM25 + structured filters (location/education/company/seniority/
  headcount). Without the flag, expansion rewrites the vector and ~halves recall.
- `search_network_pipeline … run --search-only` — read-only hybrid (BM25+vector) scoped retrieval + Postgres hydrate.
- `llm_filter_candidates` — cheap conservative triage. `evaluate_profile_candidates` — canonical
  judge rubric + IC seniority gates. `export_candidate_shortlist` — sendable shortlist.
