# Agentic search → judged ground truth (foundational)

_Created: 2026-06-26_

This is the foundational "agentic search" method behind `$recruit`. It builds a **trustworthy,
within-corpus, judged ground-truth set** for a JD from a Powerset set, then lets the cheaper
default harness be measured against it and tuned over **epochs** until it converges.

## Did this use a TurboPuffer "glob"? No — it uses our primitives.

TurboPuffer has **no glob/regex**. The "grep/glob" intuition is realized as **BM25 token
search** (`phrase_tokens` stemmed phrases + `word_tokens` words/bigrams), **fused with vector
kNN** (semantic) via reciprocal-rank fusion — i.e. *hybrid* retrieval. We never call TurboPuffer
directly. Every probe runs through the existing primitive:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
  --query "<short label>" \
  --payload-json <probe>/payload.json \
  --ledger <probe>/ledger.json \
  --search-only --limit 80 --top-k 4000
```

- `payload.json` = `{"semantic_query": "...", "bm25_queries": ["...", ...], "set_id": "<set>"}`
- Set scoping: `set_id` → Postgres `operator_ids` → TurboPuffer filter
  `allowed_operator_ids ContainsAny [...]` (warm-network only).
- `--search-only` skips **all** LLM filter/rerank → retrieval (TurboPuffer, read-only) +
  hydration (`hydrate_people`, **Postgres only**, no paid API). **~zero OpenAI spend.**

**So the "agentic search side" = orchestration on top of `search_network_pipeline`, not a new
backend.** The intelligence is in *how many* and *which* probes get issued, and in the judging.

### Footgun (this is what made the prior Codex session flail)
`search_network_pipeline` writes to a **shared ledger** by default and will silently **resume a
stale prior run** (you get someone else's results). **Always pass a unique `--ledger` per probe.**

## The method (what `$recruit` automates)

```
JD ──► PLAN (traits + seniority policy)
   │
   ├─► SOURCE  (agentic, recall-leaning, FREE)
   │     N sub-agent "sourcers", one per probe family:
   │       schedulers/control-plane · routing/traffic · inference-infra ·
   │       observability/perf · infra-company + expand-from-anchor
   │     each: writes 3-6 probe payloads (synonyms / tool-evidence / metro /
   │           soft-seniority), runs --search-only, reads results, expands from
   │           any strong anchor. Gating is DEFERRED to the judges.
   │
   ├─► MERGE   (dedupe union by person_id; attach full profile + lane provenance)
   │
   ├─► JUDGE   (mixture-of-judges — the missing stage)
   │     M independent judges (talent-analyst / recruiter / hiring-manager),
   │     each reads the CANONICAL rubric
   │       packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py
   │     and scores EVERY candidate with the house seniority hard-gates.
   │
   └─► CONSENSUS (judge_consensus.py: majority in-band & majority not-out → stack-rank)
         → ground_truth_ranked.json  (the gold yardstick)
```

Why a **panel** instead of one pass: consensus = trustworthy labels (that's what makes a set
usable as *ground truth*); dissent flags candidates for review. Judges run as Claude sub-agents,
so the whole pipeline is Claude-token-priced, not OpenAI-priced.

### Recall vs. precision split
Sourcing is deliberately **high-recall** (soft/no seniority & location filters at retrieval,
wide title synonyms, tool-evidence probes, deeper `top_k` only on productive lanes). All the
**precision** (IC seniority/track hard-gates, center-of-gravity) happens in the **judge** stage.
This is the fix for the prior run's "too-hard retrieval filters" miss mode.

## Epochs & convergence (measuring ground-truth gaps)

Ground truth (the thorough agentic+panel run) is the **yardstick**. Each cheaper/tuned harness
attempt is an **epoch**; we score it against ground truth and converge.

```
.powerpacks/recruit/<jd-slug>/
  ground_truth/ground_truth_ranked.json     # gold (built once by the full method)
  epochs/
    epoch-01/{config.json, candidates.jsonl, gaps.json}
    epoch-02/{...}
  convergence.csv                            # one row per epoch (appended)
```

- `config.json` — what changed this epoch (probe set, judge prompts, top_k, seniority policy).
- `gaps.json` — `score_ground_truth_gaps.py` output: recall@k, precision@k, gate-error,
  **which GT people were missed** (and where they dropped), plus net-new finds.
- `convergence.csv` — recall@10/@25, precision@10, gate-error, cost, n_missed per epoch, so you
  can watch the curve flatten toward 1.0 recall as prompts/probes improve.

No ledgers / run-ids / parallel state stores (repo rule) — just per-epoch dirs + appended CSV.

## Primitives this method uses

| step | primitive | spend |
| --- | --- | --- |
| source | `search_network_pipeline … run --search-only` (hybrid TurboPuffer + Postgres hydrate) | ~none |
| merge | `merge_candidate_frontier` (or inline union) | none |
| judge | Claude sub-agents on the canonical rubric (`evaluate_profile_candidates` SYSTEM_PROMPT) | Claude tokens |
| consensus | `recruit/judge_consensus.py` | none |
| score | `recruit/score_ground_truth_gaps.py` | none |

The canonical `evaluate_profile_candidates.py` (gpt-5.4) remains available as a paid,
deterministic cross-check — but the default loop is Claude-priced.

## Reproduce the AgentMail run

See `recruit-ground-truth-status.md` for the v1 numbers (79 union → 31 consensus-strong →
top-10 unanimous, ~zero OpenAI). The brief + payloads live (gitignored) under
`.powerpacks/recruit/agentmail-distsys-mts-20260626/`.
