# `$recruit` — ground-truth run v1 status (AgentMail Distributed Systems)

_Created: 2026-06-26_

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

## Next (hill-climb / ralph loop — see `recruit-skill-plan.md`)

This GT set is now the yardstick. Next: run the *default* `search-profile`/`$recruit` harness
against the same set and score it vs this GT (recall@stage, precision@k, gate-error, cost,
lane contribution); have high-reasoning models adjust the probe construction + judge prompts;
loop. Then port the `search-highlight` harness onto `main` and wire the mixture-of-judges +
expand-from-anchor stages in as first-class steps.
