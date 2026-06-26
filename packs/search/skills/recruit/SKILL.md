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
   Keep top ~80–150 per probe (recall is the goal; the judge owns precision). Dispatch the probe
   families as parallel sub-agents (Claude-priced) that read results and **expand-from-anchor**
   (seed a new probe from a strong hit's company/skills). Diversity must come from the
   **decomposition** (orthogonal, work-described seeds) — expansion homogenizes, so vary the seeds.

3. **Merge** the union by `person_id`, attaching full hydrated profiles + lane provenance
   (which probe families surfaced each).

4. **Mixture-of-judges (the precision stage).** Three independent Claude judges
   (talent-analyst / recruiter / hiring-manager), each reading the canonical rubric
   (`packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py`
   SYSTEM_PROMPT) and scoring **every** candidate with the house seniority hard-gates. Write one
   `judges/<name>.jsonl` per judge (`person_id, name, seniority_fit, in_band, verdict, score,
   rationale`).

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

## Cost

Retrieval (TurboPuffer) is read-only and hydration is Postgres-only → sourcing is ~free.
Judges run as Claude sub-agents → Claude-priced, **~zero OpenAI**. The canonical gpt-5.4
`evaluate_profile_candidates` remains available as a paid deterministic cross-check.

## Artifacts (gitignored under `.powerpacks/recruit/<jd-slug>/`)

`BRIEF.md` · `probes/<family>/…` · `candidates_union.jsonl` · `judges/*.jsonl` ·
`shortlist/{consensus.json,ground_truth_ranked.json}` · `epochs/<epoch>/{config,candidates,gaps}.json` ·
`convergence.csv`. Candidate PII stays gitignored; surface the shortlist to the user.

## Primitives

- `search_network_pipeline … run --search-only` — shotgun retrieval (hybrid BM25+vector, scoped).
- `merge_candidate_frontier` — union/dedupe (or inline).
- `recruit/judge_consensus.py` — combine judges → consensus shortlist.
- `recruit/score_ground_truth_gaps.py` — epoch scoring + convergence.
