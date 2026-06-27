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

## Flow

1. **Plan archetypes.** From the JD, design **many** distinct candidate archetypes (≈5 families ×
   2–4 angles = 10–18 probes), not one query. Cover role synonyms, sub-skills, tool/evidence,
   adjacent strong-signal companies. Defer seniority/location gating to the judges (keep
   retrieval loose).

2. **Shotgun source (FREE, read-only).** One probe = one payload. Two ways:
   - **Hand payload** (max control): `{"semantic_query":"<rich work-described sentence>",
     "bm25_queries":["...",...],"set_id":"<set>"}` → `run --payload-json … --search-only`.
   - **Deterministic via expansion** (preferred for repeatability): write each probe as a rich
     NL query and run `prepare --query "<rich query>" --preserve-query-semantic`. This keeps the
     raw query as the `semantic_query` (vector) and uses `expand_search_request` only to add BM25
     synonyms **and structured filters** (location, education, company, seniority, headcount).
     **Critical:** without `--preserve-query-semantic`, expansion rewrites the vector into generic
     prose and homogenizes BM25, which collapses probes into one neighborhood and ~halves recall
     (measured 58% vs 77% on identical seeds).
   Always run with `--search-only` (no LLM) and a **UNIQUE `--ledger`** (shared ledger silently
   resumes a stale run — the #1 footgun):
   ```bash
   uv run --env-file .env --project . python \
     packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
     --query "<label>" --payload-json <probe>/payload.json --ledger <probe>/ledger.json \
     --search-only --limit 80 --top-k 4000
   ```
   Keep top ~150–200 per probe (recall is the goal; the judge owns precision). Dispatch the probe
   families as parallel sub-agents (Claude-priced) that read results and **expand-from-anchor**
   (seed a new probe from a strong hit's company/skills). Diversity must come from the
   **decomposition** (orthogonal, work-described seeds) — expansion homogenizes, so vary the seeds.

   **Then de-homogenize BM25 (default).** After preparing all probe payloads, drop the shared
   lead BM25 terms so the probes stop retrieving the same people (data-driven, generalizes):
   ```bash
   uv run --project . python packs/search/primitives/recruit/diversify_probe_bm25.py \
     --payloads <run>/probes/*/payload.json --out-dir <run>/probes_diversified
   ```
   Run the diversified payloads. Measured lift: ~90% → 97% recall at top-200 (the distinctive
   BM25 terms help; only the shared heads hurt).

3. **Merge** the union by `person_id`, attaching full hydrated profiles + lane provenance
   (which probe families surfaced each).

4. **Two-tier judging (the precision stage).** The full panel on a huge union is expensive, so:
   - **Tier 1 — cheap triage (default for pools > ~120):** one lenient sub-agent reads the union
     from metadata and drops only clear non-matches (recruiters, sales, pure investors,
     non-technical PMs, off-domain). Keep borderline; the panel decides. Then cap to a high-signal
     pool (~100, prioritizing multi-probe hits) so panel quality stays high.
   - **Tier 2 — mixture-of-judges:** three independent Claude judges (talent-analyst / recruiter /
     hiring-manager), each reading the canonical rubric
     (`packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py`
     SYSTEM_PROMPT) and scoring **every** survivor with the house seniority hard-gates. Write one
     `judges/<name>.jsonl` per judge (`person_id, name, seniority_fit, in_band, verdict, score,
     rationale`).
   For small pools (≤ ~120) skip Tier 1 and run the panel directly.

5. **Consensus + rank:**
   ```bash
   uv run --project . python packs/search/primitives/recruit/judge_consensus.py \
     --judges-dir <run>/judges --union <run>/candidates_union.jsonl --out-dir <run>/shortlist
   ```
   → `ground_truth_ranked.json` (consensus-strong, stack-ranked) = the shortlist. Default gate:
   majority in-band AND majority not-out.

6. **Expand the good pools.** Take the top 1–2 **judged-strong** candidates and run
   expand-from-anchor probes seeded from their profile to pull similar people up; re-judge the
   net-new. (Anchor-level expansion beats family-level heuristics: a family that looks weak at
   top-40 can still hold strong candidates deeper — e.g. an inference probe surfaced a top hire
   only at rank 71.)

7. **Measure convergence (epochs).** Score any run against a trusted ground-truth set and track
   it so successive tunings converge:
   ```bash
   uv run --project . python packs/search/primitives/recruit/score_ground_truth_gaps.py \
     --ground-truth <run>/ground_truth/ground_truth_ranked.json \
     --epoch-candidates <epoch>/candidates.json \
     --epoch-dir <epoch> --epoch-label epoch-NN --convergence-csv <run>/convergence.csv
   ```
   `gaps.json` = recall@k / precision@k / missed-GT; `convergence.csv` = one row per epoch.

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

Retrieval (TurboPuffer) is read-only and hydration is Postgres-only → sourcing is ~free.
Judges run as Claude sub-agents → Claude-priced, **~zero OpenAI**. The canonical gpt-5.4
`evaluate_profile_candidates` remains available as a paid deterministic cross-check.

## Artifacts (gitignored under `.powerpacks/recruit/<jd-slug>/`)

`BRIEF.md` · `probes/<family>/…` · `candidates_union.jsonl` · `judges/*.jsonl` ·
`shortlist/{consensus.json,ground_truth_ranked.json}` · `epochs/<epoch>/{config,candidates,gaps}.json` ·
`convergence.csv`. Candidate PII stays gitignored; surface the shortlist to the user.

## Default recipe (validated; ~97% recall on 2 different JDs, filters intact, ~zero OpenAI)

decompose JD → ~15–18 **diverse, work-described** seeds → `prepare --preserve-query-semantic`
(raw query as vector + expansion BM25 + filters) → `diversify_probe_bm25` (drop shared lead
terms) → `run --search-only` top-150/200, unique `--ledger` per probe → merge union → two-tier
judging (triage if large → 3-judge panel) → `judge_consensus` → `score_ground_truth_gaps`.

## Primitives

- `search_network_pipeline … prepare --preserve-query-semantic` — expansion that keeps the raw
  query as the semantic vector + adds BM25 + structured filters (location/education/company/
  seniority/headcount). Without the flag, expansion rewrites the vector and ~halves recall.
- `search_network_pipeline … run --search-only` — shotgun retrieval (hybrid BM25+vector, scoped).
- `recruit/diversify_probe_bm25.py` — drop shared/homogeneous BM25 lead terms across the probe set.
- `merge_candidate_frontier` — union/dedupe (or inline).
- `recruit/judge_consensus.py` — combine judges → consensus shortlist.
- `recruit/score_ground_truth_gaps.py` — epoch scoring + convergence.
