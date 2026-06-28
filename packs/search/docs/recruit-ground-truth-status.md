# `$recruit` — ground-truth run v1 status (AgentMail Distributed Systems)

_Created: 2026-06-26_

_Changelog:_
- _2026-06-27: Added the fully-primitive, portable OpenAI-judge run + mixture-of-judges
  hill-climb (no sub-agents). See "Full-chain portable run + mixture hill-climb (2026-06-27)"
  at the end. Two new bridge primitives: `build_eval_inputs.py`, `triage_candidates.py`._
- _2026-06-28: Added a FREE portable judge (`codex_judge.py`, spawns `codex exec`, reuses the
  canonical rubric+scorer) and the key calibration finding: recall-vs-GT was capped by the
  shortlist CUTOFF, not sourcing or judge vendor. See "Free codex judge + threshold
  calibration (2026-06-28)" at the end. New knob: `judge_consensus --score-threshold`._

_PII-free status for review. Candidate identities (names + LinkedIn + per-judge rationales)
are surfaced to the requester in chat and kept under gitignored
`.powerpacks/recruit/agentmail-distsys-mts-20260626/` — they are not committed._

## What this run proves

A trustworthy **within-corpus, judged** ground-truth set can be built for a JD using only
existing `main` primitives + Claude sub-agents, at **~zero OpenAI spend**. This is the
correct baseline the prior Codex session failed to produce (it used off-corpus Harmonic CSV
grep and never judged the labels — see `recruit-skill-plan.md`).

## JD

Member of Technical Staff – Distributed Systems @ AgentMail (San Francisco, **IC**).
Schedulers / control plane / global routing / traffic management / LLM inference routing
(KV cache across GPU·CPU·NVMe) / deep observability. Bonus: vLLM/SGLang, GPUs.
`https://jobs.ashbyhq.com/AgentMail/6e99881b-595c-44e0-8f82-eb431ef98623`

## Method (5 sourcers → merge → 3 judges → consensus)

1. **Agentic sourcing**, scoped to Powerset set `2663f70d…`, read-only TurboPuffer (hybrid
   BM25+vector) + Postgres hydration via
   `search_network_pipeline … run --search-only` (no LLM, no spend). Five parallel Claude
   sourcers, one per probe family: schedulers/control-plane, routing/traffic, inference-infra,
   observability/perf, infra-company+expand-from-anchor. Each ran 3–6 adaptive probes.
2. **Merge/dedupe** to a union, attaching full hydrated profiles + lane provenance.
3. **Mixture-of-judges**: three independent Claude judges (talent analyst / recruiter / hiring
   manager), each reading the canonical rubric
   (`evaluate_profile_candidates.py` SYSTEM_PROMPT) and scoring **every** union candidate with
   the house seniority hard-gates.
4. **Consensus stack-rank**: ground truth = majority in-band AND majority not-out, ranked by
   mean judge score.

### Recipe footgun fixed (this is what tripped Codex)
`search_network_pipeline` uses a **shared ledger** and silently resumes a stale prior run.
Every probe MUST pass a unique `--ledger`. Documented in the run BRIEF and the plan.

## Results (metrics — see `.powerpacks/recruit/.../metrics.json`)

- Probe families: **5** · union unique candidates: **79**
- Consensus strong (≥2/3 in-band & ≥2/3 not-out): **31**
- **Top 10 are unanimous** (3/3 in-band, 3/3 not-out) → high-confidence gold labels.
- Seniority gating did real work (this is an IC role): per judge, ~32–33 of 79 were
  hard-gated `too_senior` (current founders/CTO/VP/Director/EM) and ~4–15 `wrong_track`
  (pure SRE without systems depth, ML-research/training-only, hardware-only).
- **Lane contribution to the ground truth:** routing 12, scheduler 8, company 6, inference 6,
  observability 4 — i.e. routing/scheduler probes were the highest-yield; observability was
  noisiest. Useful signal for tuning the default `$recruit` probe mix.

## Read on quality

The unanimous top of the list is squarely on-target (e.g. the #1 is the principal-IC author
of a well-known OSS cluster scheduler; #2 an inference-serving-systems MTS at a frontier lab;
#3 a scheduling Senior Staff at a hyperscaler). The judges independently converged, and the
seniority gate correctly demoted several deepest-on-paper distsys people whose **current**
role is founder/exec — exactly the IC discipline the rubric demands.

## Artifacts (gitignored, under `.powerpacks/recruit/agentmail-distsys-mts-20260626/`)

`BRIEF.md` · `candidates_union.jsonl` · `judges/{talent_analyst,recruiter,manager}.jsonl` ·
`consensus_all.json` · `ground_truth_ranked.json` · `ground_truth_top10.md` ·
`stage_matrix.csv` · `metrics.json` · `probes/<family>/…` (payloads + ledgers).

## Epoch tracking (convergence)

The thorough agentic + 3-judge run is the **gold yardstick** (`ground_truth/ground_truth_ranked.json`,
31 strong). Each cheaper/tuned harness attempt is an **epoch** scored against it by
`recruit/score_ground_truth_gaps.py`, appending a row to `convergence.csv`.

### Recall diagnostic (answers: "is the pipeline bad at recall, or are we misusing it?")

**Misusing it — recall is excellent; ranking + keep-depth is the lever.** A *single* naive broad
probe at full depth contained **31/31** ground-truth people (ranks 5 → 5089). The pipeline finds
everyone; one query's ranking just buries them, so a top-50 cap keeps ~16%. Diverse ("shotgun")
probes fix it: each targeted probe pulls *its* relevant GT to the top (Kan Wu 3323→71, Sharma
Podila 841→16, Kourosh 3830→41).

### Convergence (`convergence.csv`)

| epoch | sourcing | pool | GT recall |
| --- | --- | --- | --- |
| 01 naive | 1 probe, keep top-50 | 50 | **16%** |
| 02 shotgun | ~18 probes, keep top-40 | 509 | **65%** |
| 03 shotgun | ~18 probes, keep top-80 | 954 | **100%** |
| 04 targeted-expansion | top-40 + deeper on productive families | 901 | **97%** |

Takeaways for the harness: (1) **shotgun for recall, judge for precision** — never tighten
retrieval to chase precision. (2) Keep ~top-80/probe (or top-40 + expand-from-anchor). (3) Pool
quality by family (top-40, judged): company/observability/routing ~6% strong, inference ~1% — but
inference still held a top hire at rank 71, so **expand at the candidate level (anchor), not by
shallow family precision**.

## Benchmark: LLM expansion vs hand-decompose (is the deterministic path good enough?)

Tested whether the existing `expand_search_request` (deterministic LLM query expansion) can
match hand-written decomposed probes for recall. Each probe run `--search-only` top-80, union,
scored vs the 31-person ground truth.

| approach | probes | pool | GT recall |
| --- | --- | --- | --- |
| expansion, single full-JD query | 1 | 80 | 16% |
| expansion, 6 archetype queries | 6 | 200 | 35% |
| expansion, 16 archetype queries | 16 | 483 | 52% |
| expansion, 16 archetypes @ top-200 | 16 | 936 | **61%** |
| **hand-decompose, ~18 probes @ top-80** | 18 | 954 | **100%** |

Findings:
- **Per probe, expansion ≈ hand** (~10–16% each). Expansion generates *excellent keywords*
  (14–16 BM25 synonyms: "cluster scheduler engineer", "vLLM engineer", "raft engineer"…).
- The gap is **not** caused by: hard filters (expansion auto-adds `seniority_bands`/company-ids,
  but stripping them changed nothing), probe count, retrieval depth (61% at hand-equal pool of
  936), or expand-from-anchor (removing the 2 anchor probes left hand recall at 100%).
- The gap **is** caused by **embedding-space diversity**. The LLM expansion produces
  *title-centric, homogeneous* probes — nearly every archetype's BM25 began with "distributed
  systems engineer" — so the probes overlap and the union saturates ~60%. The hand probes used
  varied *work-described* semantic queries that spanned more of the space.

Implication for `$recruit`: keep `expand_search_request` for deterministic *keyword* generation,
but drive **diversity at the decomposition layer** (orthogonal axes: work-described not
title-described, specific tech stacks, specific company tiers/problem domains) so the probe set
covers the space. Precision is owned by the judge panel regardless. Pure auto-expansion of one
(or a few title-y) queries tops out ~60% recall; diverse decomposition is what reaches 100%.

### Controlled test: same seeds, expansion on vs off (the translation is lossy)

To isolate the expansion step, the **same 21 hand-probe intents** were fed two ways, top-80, union:

| same 21 seeds | pool | GT recall |
| --- | --- | --- |
| raw (rich query used verbatim as `semantic_query` + hand BM25) | 954 | **100%** |
| expansion-rewritten | 510 | **58%** |

Same seeds, same count — the only variable is expansion, and recall fell 100%→58% (pool shrank
954→510 = more overlap). Why: expansion **genericizes the semantic_query** (e.g.
"…building high-performance schedulers, control planes, global request routing…" →
"Engineers specializing in distributed systems design and implementation…") and leads every
BM25 list with "distributed systems engineer", so distinct intents collapse into the same
embedding neighborhood.

**Fix (deterministic, = the hand recipe):** use the NL seed **verbatim as `semantic_query`** and
use `expand_search_request` only to *add* BM25 synonyms — never to rewrite the semantic vector.
The vector side needs the specific wording to spread across the space. A fixed template that
emits ~18 diverse work-described seeds → each used directly as the semantic vector (+ expansion
BM25) → judge reproduces hand-level recall without hand authoring.

### Shipped fix + validation: `--preserve-query-semantic`

Implemented as a flag on `search_network_pipeline prepare` (helper
`pin_payload_semantic_query` in `shared/seniority_bands.py`): keep expansion's BM25 **and all
structured filters** (location, education, company, seniority, headcount), but set
`semantic_query` to the raw `--query`. Re-ran the **same 21 hand seeds** through it:

| same 21 seeds | pool | GT recall |
| --- | --- | --- |
| expansion (lossy rewrite), top-80 | 510 | 58% |
| **preserve-semantic, top-80** | 507 | **77%** |
| **preserve-semantic, top-150** | 900 | **90%** |
| raw hand, top-80 | 954 | 100% |

Preserving the vector recovers most of the loss (58→77→90%). The residual ~10% vs raw-hand is
the **BM25 channel**: expansion's BM25 is title-centric (every list leads "distributed systems
engineer"), so its fusion contribution overlaps rather than diversifies — the 3 still missed at
top-150 (Kourosh, Assaf, Rohun) were hand-found via *varied* BM25 probes. Net: the deterministic
`--preserve-query-semantic` path + deeper keep + judge gets to ~90% with no hand authoring;
closing the last 10% means diversifying BM25 too (or deeper keep / anchor expansion).

## Hill-climb continued: closing the residual 10% (epoch-13)

The preserve-semantic residual was the BM25 channel. Re-ran the 21 seeds varying the BM25:

| variant (preserve-semantic) | top-150 | top-200 |
| --- | --- | --- |
| full BM25 | 90% | — |
| pure-vector (no BM25) | 84% | 90% |
| **BM25-diversified (drop shared lead terms)** | **94%** | **97%** |

Dropping only the homogeneous lead terms ("distributed systems engineer", "infrastructure
engineer", "member of technical staff") while keeping the distinctive BM25 terms reaches **97%**
(epoch-13). So distinctive BM25 *helps*; only the shared heads hurt. Deterministic recipe now:
preserve-semantic + drop-shared-BM25 + top-200 + judge ≈ hand-level, filters intact.

## Generalization: a second, different JD (applied-AI, not distsys)

Validated the tuned recipe on **"Founding Applied AI Engineer"** (LLM products / RAG / agents —
a different role shape) against the same Powerset set. Deterministic preserve-semantic shotgun
(6 work-described seeds, drop-shared-BM25, top-150) → **339** union → **two-tier judging**
(cheap triage 339→279, then 3-judge panel on a 100-cap high-signal pool) → consensus.

Result: **16 consensus-strong, top-10 unanimous** (3/3 in-band). The IC seniority gate again did
the heavy lifting — most AI people in this network are *current founders/CTOs* → correctly
`too_senior` for an IC role. Clean convergence on a role with no shared vocabulary with the first
JD confirms the recipe (decompose → preserve-semantic shotgun → mixture-of-judges → consensus)
is **not overfit to distributed systems**. Two-tier judging also validated as the cost-saver
(triage drops the bulk cheaply before the expensive panel).

### JD#2 hill-climb against an INDEPENDENT thorough ground truth (not circular)

To measure JD#2 honestly (guardrail #3), a thorough GT was built **independently** of the
deterministic recipe: 5 hand-crafted sourcers (~25 diverse probes + expand-from-anchor) → 75
union → 3-judge panel → **45 consensus-strong** (top-10 unanimous: Cooper Raterink, Hanson Wang,
Gabor Angeli, Lucy Zhang, Saqib Ameen, …). Then the deterministic recipe was scored against it:

| JD#2 epoch | sourcing | pool | recall vs independent GT (45) |
| --- | --- | --- | --- |
| 01 deterministic, 6 seeds | 6 | 339 | 36% |
| 02 deterministic, 18 diverse seeds + preserve-semantic + diversify, top-200 | 18 | 1856 | **87%** |

Same lever as AgentMail — **seed diversity/count** — now confirmed against a GT produced by a
*different* process, so it is not circular and not overfit. The 18 seeds were written from the JD,
**not** reverse-engineered from the misses (guardrail). The 6 still missed at top-200 (e.g. a
Chroma founding eng, an xAI MTS) are anchor/company-reachable; closing 87→~95% is the
**expand-from-anchor** lever seeded from the recipe's *own* judged-strong (never from the GT).

## New primitives + docs in this PR

- `packs/search/docs/agentic-search.md` — the foundational agentic-search method (answers
  "glob or primitives?": it's `search_network_pipeline --search-only`, hybrid BM25+vector, not glob).
- `packs/search/primitives/recruit/judge_consensus.py` — combine N judge JSONL → consensus
  stack-rank + ground-truth set (reproduces this run's 31-strong / top-10-unanimous result).
- `packs/search/primitives/recruit/score_ground_truth_gaps.py` — score an epoch vs ground truth
  (recall@k, precision@k, missed GT ids) + append to `convergence.csv`.
- `tests/test_recruit.py` — unit tests for both (7 tests, green).

## Next (hill-climb / ralph loop — see `recruit-skill-plan.md`)

This GT set is now the yardstick. Next: run the *default* `search-profile`/`$recruit` harness
against the same set and score it vs this GT (recall@stage, precision@k, gate-error, cost,
lane contribution); have high-reasoning models adjust the probe construction + judge prompts;
loop. Then port the `search-highlight` harness onto `main` and wire the mixture-of-judges +
expand-from-anchor stages in as first-class steps.

## Full-chain portable run + mixture hill-climb (2026-06-27)

Ran the **entire `$recruit` chain as callable primitives — zero sub-agents, zero harness
improvisation** — and hill-climbed the judge stage. This closes the portability gap (Codex / any
harness reproduces it): `decompose_jd` → `run_shotgun` → `build_eval_inputs` →
`triage_candidates` → `evaluate_profile_candidates` (gpt-5.4) → `judge_consensus` →
`score_ground_truth_gaps`. Artifacts under
`epochs/epoch-15-judged-fullchain/`. **Spend ≈ $47** (3 gpt-5.4 judge passes dominate; sourcing
+ triage + plan ≈ $0.3).

### Two new bridge primitives this required

- **`build_eval_inputs.py`** — the shotgun emits `union.jsonl`, but the canonical judge reads a
  profile-search run dir (`plan.json` + `candidate_frontier.jsonl` + `probe_summaries.json` →
  the already-on-disk `profiles.jsonl.gz`). This adapter rewrites the run into that contract with
  **no recompute** (1 cheap LLM call extracts must/nice traits from the JD). Verified 1034/1034
  profile coverage.
- **`triage_candidates.py`** — the existing `llm_filter_candidates` is welded to a
  `search_network_pipeline` run-state (merge step + hydration coverage), so it does **not** fit
  the recruit union. This is the portable tier-1 filter over `candidate_frontier.jsonl`: cheap
  `gpt-4.1-mini`, conservative (`keep`/`maybe` survive), reads the **profile, not probe count**.

### Why not cap by probe count (recall guardrail, measured)

Capping the judge pool by `found_by` count is tempting but **wrong**: 8 of 28 present GT are
**single-probe** hits ranked 658–940 by probe count (e.g. Sharma Podila, found only by the
`company` probe, rank 940). Any affordable probe-count cap throws away GT. Triage must look at
the profile. (`triage 1034 → 606` kept 26/28 present GT.)

### End-to-end results vs the 31-person judged GT

| stage | pool | GT recall | precision@25 | note |
| --- | --- | --- | --- | --- |
| sourcing (epoch-14) | 1034 | 0.90 | — | recall is NOT the bottleneck |
| → triage | 606 | 0.84 of pool | — | conservative; lost 2 GT |
| single gpt-5.4 judge (epoch-15) | 58 strong | 0.48 | 0.36 | one strict judge |
| **3-judge mixture, (2,2) gate (epoch-16)** | 62 strong | **0.52** | **0.44** | majority in-band AND not-out |

The end-to-end shortlist recall (52%) is **judge-bounded, not retrieval-bounded** — sourcing
already contains 90% of GT. Of the 16 missed: 3 not in pool, 2 dropped in triage, 11 judged-out
— and **8 of those 11 were NOT seniority-gated** (5 "ideal", 3 "acceptable"), marked out on
borderline trait score (jd 0.39–0.51). That borderline band is exactly where single-judge
variance lives, so the **mixture is the right lever** (it lifted recall@10 0.13→0.16 and
precision@10 0.40→0.50 — the top of the list improved most).

### Hill-climb finding: the consensus GATE is a Pareto lever (NOT yet default)

3-judge panel, gate sweep (free — pure re-aggregation):

| gate (in-band, not-out) | strong | overall recall | p@25 | p@50 |
| --- | --- | --- | --- | --- |
| single judge | 58 | 0.484 | 0.36 | 0.30 |
| (2,2) majority — canonical | 62 | 0.516 | 0.44 | 0.28 |
| **(2,1) majority-in-band + ≥1 not-out** | 79 | **0.645** | **0.48** | **0.34** |
| (1,1) | 83 | 0.645 | 0.48 | 0.34 |

`(2,1)` **Pareto-beats** the canonical `(2,2)` — higher recall *and* precision — because it
rescues borderline GT that one strict judge cut while another kept (real positives, so precision
rises too). **Per the anti-local-maxima rule this stays NON-default until validated on a 2nd
structurally-different JD** (the founding-applied-AI JD has an independent GT ready). Did not flip
the `judge_consensus` default; recorded the finding only.

### GT itself under-counts quality

The independent 31-person GT was built by a Claude 3-judge panel; the gpt-5.4 panel surfaces
strong people the GT simply missed — its top "net-new" (non-GT) picks are an NVIDIA AI/HPC infra
eng, a Cursor SWE, a Meta production engineer, a Pinterest AI-infra eng. So "recall vs GT"
**understates** true shortlist quality; treat it as a lower bound.

### Updated primitive inventory (added this run)

- `build_eval_inputs.py`, `triage_candidates.py` (above).
- `judge_consensus.py` now ingests the `evaluate_profile_candidates` raw format directly
  (`candidate_id`→`person_id`, `jd_score`→`score`, `in_band` derived from `seniority_fit`) so a
  judges dir can mix OpenAI-judge and Claude-sub-agent verdicts.
- `tests/test_recruit.py` — 33 tests green (added build_eval_inputs / triage_candidates /
  normalize_verdict coverage).

### Next

Validate the `(2,1)` gate on the founding-applied-AI JD's independent GT (one 3-judge panel run,
~$45) before promoting it to default; if it Pareto-wins there too, flip the `judge_consensus`
default and add an `expand_from_anchor` round to lift the 3-not-in-pool sourcing gap.

## Free codex judge + threshold calibration (2026-06-28)

Goal this round (user): make the **primitives** reproduce the original recall/precision and
hill-climb — **cheap/free first**, spawning `codex`/`claude` CLI subprocesses from a primitive
instead of the paid gpt-5.4 API.

### New primitive: `codex_judge.py` (free, portable)

Spawns one `codex exec` subprocess per candidate. It is a **thin shim**: it reuses the canonical
`SYSTEM_PROMPT`, `build_user_prompt`, frontier/profile loaders, AND `normalize_evaluation`
(deterministic scorer) from `evaluate_profile_candidates` — so the **bar is byte-identical** to the
paid judge; only the inference engine differs. Runs on ChatGPT-subscription auth → **$0 per token**.
Output is the same `candidate_evaluations.raw.jsonl`, so `judge_consensus` ingests it unchanged.
~20 s/candidate; 8-way concurrency. (Gotcha fixed: passing `--output-schema` conflicts with the
rubric's own OUTPUT contract and makes the model return all-"out" — don't; rely on the rubric +
robust JSON extraction.) This matches the approach the user had sketched in the original Codex
session: "call codex in another shell to eval candidates, reuse canonical prompts/rubrics."

### The real finding: recall was capped by the SHORTLIST CUTOFF, not the pipeline

Head-to-head on a fair 68-candidate blind pool (26 GT + 42 non-GT), codex-low vs gpt-5.4-low,
**same rubric + same scorer**:

| judge (low effort) | GT recall | non-GT admitted |
| --- | --- | --- |
| codex (free) | 0.46 | 3 / 42 |
| gpt-5.4 (paid) | 0.62 | 2 / 42 |
| cross-vendor (either keeps) | 0.65 | — |

So swapping vendor did NOT recover the original ~0.9 — because **both** strict judges reject ~40–50%
of the GT. The GT was built by a *lenient* Claude panel; the canonical rubric/scorer is *strict by
design* ("default disposition is OUT"). The grader and the answer key are calibrated differently.

The fix is the **shortlist cutoff**, swept offline for free over the per-candidate scores we
already have (the verdict is just a threshold on the canonical `jd_score` + the seniority gate):

| score cutoff | codex recall / non-GT | gpt recall / non-GT | union recall / non-GT |
| --- | --- | --- | --- |
| 0.50 (≈ default verdict cutoff) | 0.62 / 3 | 0.50 / 2 | 0.69 / 3 |
| **0.40** | **0.88 / 4** | 0.69 / 3 | 0.88 / 5 |
| 0.35 | 0.92 / 5 | 0.81 / 3 | 0.92 / 6 |
| 0.30 | 0.92 / 8 | 0.85 / 4 | **0.96 / 9** |

**Conclusion:** the primitives DO reproduce ~0.9 recall. The lever is the cutoff, not sourcing,
vendor, or a bug. Lowering the recruit shortlist cutoff from ~0.50 to ~**0.40** recovers
**0.88–0.92 recall while admitting only 4–6 non-GT of 42** (and several of those "non-GT" are
strong people the GT itself missed). Cross-vendor union pushes ~0.96. This is exposed as
`judge_consensus --score-threshold` — it chooses the cutoff on the canonical score and does NOT
weaken the rubric/scorer.

### Local-maxima discipline

`--score-threshold` defaults to OFF (canonical not-out gate). The ~0.40 sweet spot is measured on
ONE JD; validate on the founding-applied-AI JD's independent GT before hardcoding it as the default.
Watch the whole curve (recall AND non-GT admits), not one number.

### Cost

This entire round (codex judge on a 68-pool + the full 606 frontier, plus the offline sweeps) is
**$0 OpenAI** — `codex exec` runs on the ChatGPT subscription. The earlier gpt-5.4 verdicts reused
from the prior round were already paid; no new API spend.

### Full-pool confirmation (606 codex judge, scored vs 31-GT)

The 68-pool sweep above is GT-enriched; this is the honest end-to-end on the FULL 606 triaged
survivors, free codex judge, scored vs the 31-GT. Ceiling is **26/31 = 0.84** (5 GT were never
sourced — seed variance). Logged as `epoch-17-*` in `convergence.csv`.

| config | shortlist | recall (of 31) | p@10 |
| --- | --- | --- | --- |
| codex t0.50 | 51 | 0.55 | 0.40 |
| codex t0.40 | 98 | 0.71 | 0.40 |
| codex t0.30 | 153 | **0.81** | 0.40 |
| cross-vendor mean(codex,gpt) t0.40 | 88 | 0.74 | **0.50** |

At t0.30 the free codex judge recovers **0.81 of the 0.84 ceiling** — essentially every GT that
was sourced; the only residual misses are the 5 not in the pool. So the primitive chain fully
reproduces the original recall once the cutoff is dialed; the remaining lever is sourcing those
last 5 (expand-from-anchor). Cross-vendor cleans the TOP of the list (p@10 0.40 → 0.50) at a
smaller shortlist. Cost: **$0** (codex on ChatGPT subscription).
