# `$recruit` — emulate a recruiting team (source → judge → hill-climb)

_Created: 2026-06-26_

_Changelog:_
- _2026-06-26: Initial amended plan. Supersedes the Codex `hillclimb_execution_plan.md`
  under `.powerpacks/search-highlight-ground-truth/`. Fixes the off-corpus ground-truth
  mistake, reframes `search-highlight` as the unmerged sibling-worktree harness, and adds
  the two missing pipeline stages (mixture-of-judges + iterative expand-from-anchor)._

## What this is

Evolve the existing recruiting search into a skill — working name **`$recruit`** — that
emulates a full recruiting team end to end for a given JD:

1. **Source** candidates from the user's Powerset network (agentic TurboPuffer search,
   scoped to a set id).
2. **Judge** them with a recruiter/manager **mixture-of-judges** panel (not a single LLM
   pass) that hard-gates seniority/track and stack-ranks the rest.
3. **Hill-climb**: surface which *kind* of profile is landing well, then source *more like
   that* (expand-from-anchor), run a few rounds, and re-rank across rounds.
4. **Evaluate the skill itself** against a trusted **ground-truth set** (did it find the
   right people, at reasonable cost, quickly, with sound reasoning), then have high-reasoning
   models adjust the prompts and loop ("ralph loop") until the result is good.

## Ground state — what actually exists today (read this before touching anything)

| Thing | Reality | Where |
| --- | --- | --- |
| `search-profile` skill | **Real, merged on `main`.** Full recruiter loop: trait extraction → 2–3 archetype probes → per-lane precision → one deep lane → merge → canonical JD evaluation → shortlist. | `packs/search/skills/search-profile/SKILL.md` |
| `search-network` pipeline | **Real, merged.** Sources from TurboPuffer (Powerset set) or local DuckDB. `prepare`/`run`, `--filter-only`, `--seniority-bands`, `--current-role`, `--limit`. | `packs/search/primitives/search_network_pipeline/` |
| Canonical JD evaluator | **Real, merged.** Bar-raiser rubric, per-trait evidence ladder, deterministic scoring, hard seniority gates. Default `gpt-5.4`, medium reasoning. | `packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py` |
| LLM rerank / filter | **Real, merged.** Conservative cheap filter + full rerank with IC-vs-exec downranking. | `packs/search/primitives/llm_rerank_candidates/`, `.../llm_filter_candidates/` |
| `search-highlight` harness | **NOT on `main`.** Lives only in the stale sibling worktree `powerpacks-highlight-orchestration` (branch `arthur/highlight-orchestration`, behind `main`). `highlight_search_pipeline.py` (1872 lines) + `search-highlight/SKILL.md` (314 lines). | sibling worktree only |
| Codex "ground truth" (Hebbia data-engineer) | **Invalid — do not trust.** Its "novel strong" picks were grepped from an **off-corpus** aleph-mvp Harmonic CSV (a different population than the searchable Powerset set), and **never run through the canonical judge**. Several aren't even in the set's TurboPuffer namespace. | `.powerpacks/search-highlight-ground-truth/` |

### Two foundational mistakes from the prior Codex session (do not repeat)

1. **Off-corpus ground truth.** Ground truth for evaluating *Powerset-set recall* must come
   **from the set's searchable corpus** (its TurboPuffer namespace, scoped by `set_id`). A
   "perfect" person who isn't in the set cannot be a recall target for the set — they only
   prove the set is incomplete, which is a different problem. The Harmonic CSV lane is a fine
   *external diagnostic*, but it is **not** ground truth for this use case and must not be the
   default "third vertical."
2. **Unjudged labels.** "strong_yes" labels were hand-rolled by sub-agents, never scored by
   the canonical rubric. Ground truth must be **judged** — ideally by a consensus panel — so
   the labels are trustworthy.

## The pipeline, and the 2 stages that are missing

Current `search-profile` flow (5 stages, all real):

1. Plan: extract traits + design 2–3 archetype probes.
2. Source: run each probe via `search-network` (TurboPuffer), shallow + `--filter-only`.
3. Pick the highest-precision lane, run it deep (full rerank).
4. Merge + dedupe the frontier.
5. Canonical JD evaluation → shortlist.

**Missing stage A — mixture-of-judges.** Today a single evaluator pass produces verdicts.
`$recruit` adds a **panel**: independent high-reasoning judges (e.g. a *talent analyst*
calibrating seniority/geo/center-of-gravity, a *recruiter* surfacing high-trajectory near
misses, a *manager* owning hard gates and the export decision). Consensus = trustworthy
label; dissent = flag for review. This is exactly what makes a **ground-truth** set credible.

**Missing stage B — iterative expand-from-anchor in the sourcerers.** `search-profile` does
*one* deep lane after the first eval. `$recruit` repeats the idea **inside sourcing across
rounds**: once 1–2 strong candidates are confirmed, generate "more like this, with missing
trait X fixed" probes, re-source (bounded), re-judge, and **stack-rank across all rounds**.
Deepen `top_k` only on lanes that recovered positives — not everywhere (cost control).

## Sourcing: agentic TurboPuffer search (the "grep/glob" the user wants)

TurboPuffer has **no regex/glob**, but its BM25 over `phrase_tokens`/`word_tokens` *is* the
text-search surface, fused with vector kNN (hybrid). Scope to the set via the
`allowed_operator_ids ContainsAny <operator_ids>` filter (resolved from `set_id` through
Postgres). "Agentic search" here means: an agent issues **many** diverse probes against the
scoped namespace (role synonyms, tool-evidence, metro expansion, soft seniority), reads what
comes back, and **adapts** the next probes — rather than one simple high-recall query.

Retrieval fixes carried over from the (valid half of the) Codex diagnostic:
- **Location:** metro-area expansion (SF Bay Area, Mountain View, Palo Alto, Oakland, …),
  soft-scored, not exact-city hard filter.
- **Seniority:** retrieve broadly (lead/principal/unlabeled included); hard-gate seniority
  **at evaluation**, not at retrieval.
- **Title synonyms:** widen beyond the literal role (for the AgentMail JD: distributed
  systems, scheduler, control plane, traffic/routing, performance engineering, inference
  infra, vLLM/SGLang, GPU/accelerator, KV cache, observability/tracing).
- **Tool/evidence probes** as first-class recall surfaces.
- **Deeper `top_k`** only on productive lanes.

## First benchmark JD

**Member of Technical Staff — Distributed Systems @ AgentMail** (San Francisco).
`https://jobs.ashbyhq.com/AgentMail/6e99881b-595c-44e0-8f82-eb431ef98623`
IC role. Must: strong distributed-systems fundamentals (concurrency, networking, databases,
performance engineering), high-performance schedulers, global routing/traffic management,
deep observability. Bonus: ML inference stacks (vLLM/SGLang), GPUs/accelerators.
**Hard gate:** current founders / C-suite / VP / head-of are `too_senior` for this IC role
(past founder OK if current role is in-band IC).

Goal: **~10 robust, judged ground-truth candidates from within Powerset set
`2663f70d-2ab7-4371-b871-2cedab5b582f`**, for the user to review.

## Ground-truth method (within-corpus + judged)

1. Build the JD plan (traits + seniority policy) from the AgentMail JD.
2. **Agentic sourcing, scoped to the set:** dispatch parallel agents issuing diverse
   TurboPuffer probes (role synonyms / tool-evidence / metro / soft-seniority), each reading
   results and expanding from anchors. Hydrate the union.
3. **Judge with the canonical evaluator** (`gpt-5.4`, medium) for deterministic scored
   verdicts, then a **3-judge consensus panel** on the finalists for trustworthy labels.
4. **Stack-rank**; take the top ~10 in-band. Record a `stage_matrix` (which probe found each,
   where each dropped) + cost.
5. (Optional, labeled separately) a **broad unscoped TP** diagnostic lane to show who exists
   beyond the warm network — never mixed into the scoped ground truth.

PII handling: raw candidate artifacts (names, LinkedIn) live under gitignored
`.powerpacks/recruit/agentmail-distsys-mts-20260626/`. The PR tracks the **plan, method,
status, metrics, and hill-climb iterations** — the finalist list is surfaced to the user for
review (tracked only if the user opts in).

## Evaluate-the-skill loop (the "ralph loop")

Once ground truth exists, treat each `$recruit` run as a version to score:
- **Metrics:** recall@{retrieved, hydrated, filtered, reranked-top-25, judged, shortlist};
  precision@{10,25}; founder/C-suite false-positive count; `too_senior` sendable violations;
  unique positives by lane; cost USD; cost per recovered strong-yes; lane contribution.
- **Diff:** `stage_matrix.csv` + `metrics.json` + `diff.md` per version.
- **Improve:** high-reasoning models (Claude + others) read the diffs and adjust the
  canonical prompts / probe construction / top-k & expansion policy. Re-run, re-score, loop
  until precision@10 and recall@judged are satisfactory without weakening the hard gates.

## Task breakdown

1. **Ground truth v1 (this PR):** AgentMail JD plan → agentic scoped TP sourcing → canonical
   judge + consensus panel → top-10 for review. Artifacts + metrics + status doc.
2. **Skill scaffolding:** port the `search-highlight` harness from the sibling worktree onto
   `main` (rebase/cherry-pick), rename/route as `$recruit`, keep canonical eval/export as the
   source of truth. (Later PR.)
3. **Stage A — mixture-of-judges:** recruiter/talent-analyst/manager judge primitives;
   consensus + dissent. (Later PR.)
4. **Stage B — expand-from-anchor sourcing loop + cross-round stack-rank.** (Later PR.)
5. **Hill-climb harness:** metrics/stage-matrix/diff + the prompt-improvement loop. (Later PR.)

## Non-goals / guardrails

- No off-corpus CSV in the ground-truth path; no mixing local SQL/Harmonic into cloud scope.
- Canonical `evaluate_profile_candidates` + `export_candidate_shortlist` stay the source of
  truth; judges add signal, they don't replace the rubric.
- No ledgers / run-ids / parallel state stores — manifest + outputs per stage (repo rule).
- Hard seniority/track gates always enforced at evaluation for IC roles.
