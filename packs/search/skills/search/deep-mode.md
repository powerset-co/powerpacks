# `$search` deep mode — the deep-search engine

`$search`'s deep mode for a JD. You load this file when the Step-1 decision you recorded is
`depth: deep` — automatic for a job-posting URL or pasted JD, or on an explicit ask ("deep",
"thorough", "build a shortlist", "recruit ...", "more people like <url>"). The decision is
already recorded in the run dir's `decision.json` (`.powerpacks/deep-search/<jd-slug>/`);
Review below is the run's one confirm-before-execute gate.

<!--
Created: 2026-06-26
Changelog:
- 2026-06-26: Initial engine. Replaces search-highlight. Built on the empirical finding that
  the existing search_network_pipeline has excellent recall (a single loose probe contains
  100% of ground truth at depth) but noisy single-query ranking — so the lever is WIDE SEARCH
  (many diverse probes) + a mixture-of-judges, not a new backend. See
  packs/search/docs/agentic-search.md and deep-search-ground-truth-status.md.
- 2026-06-29: Add the CORE-GATE + Review. build_eval_inputs tags must-haves core|table_stakes;
  judge_consensus --plan gates the shortlist on genuinely doing >=1 core domain capability (not the
  blended score, which can't separate "filled" from "give-up"). One human touchpoint (Review, the
  plan), then autonomous. Measured: AgentMail distsys -> 88 filled; Realta fusion VP -> 12->7->2.
- 2026-06-30: Absorb the old $search-profile inputs. deep_search_loop accepts a job-posting URL via
  --jd-url (fetch_jd.py, stdlib/no spend) in addition to --jd-file.
- 2026-07-01: Fold into $search as its deep mode. Removed the separate $recruit / $search-profile
  skills; renamed the engine package recruit/ -> deep_search/ (recruit_loop -> deep_search_loop,
  run_shotgun -> run_wide_search) and the route recruit -> deep.
- 2026-07-01: Entry is now the agent-made Step-1 decision (`depth: deep` in decision.json), not
  the deleted route_query classifier. Review doubles as $search's universal pre-execute gate.
- 2026-07-03: Present the shortlist with a ~0.55 SENDABLE CUT (measured on the AgentMail rerun:
  the core-gated tail below ~0.55 was padding); keep the full score-only pool (consensus.json,
  mean >=0.40 + in-band, no core-gate) as the bench to mine. Judge rubric gained research-evidence,
  lead-IC, consultant-enforcement, split-focus, and ambiguous-title rules (evaluate_profile_candidates).
- 2026-07-08: Rename the human checkpoint "GATE 1" -> "Review" in the printed task checklist and
  prose (the plan-approval step). The algorithmic core-gate (which candidates make the shortlist)
  keeps its name. Engine flags are unchanged (--plan-approved / awaiting_plan_approval).
-->

> **This is `$search`'s deep mode.** Job-posting URLs, pasted JDs, complex role briefs, "build a
> shortlist", and "more people like <url>" all land here via the recorded Step-1 decision —
> JD→judged-shortlist with a core-tagged plan, mixture-of-judges, core-gate, and IC-track-aware
> seniority.

## The engine

Source, judge, and rank candidates for a JD from a Powerset set, the way a sourcing team would. This
is the productized version of the agentic-search method in `packs/search/docs/agentic-search.md`.

## Run it: ONE human gate, then autonomous

Track the run as **native harness tasks (checkboxes)** so progress is visible/resumable. There is
exactly **one human touchpoint — Review, the plan** — and everything after it runs to a finished,
ranked shortlist with no further prompts (judge auto, no spend confirm; expand-from-anchor auto).

```
☐ 1. Source + extract plan      robust_source → build_eval_inputs   (free / 1 cheap call)
      ──▶ Review: show the plan, get approval/edits   ← the ONLY human touch
☐ 2. Judge → core-gate → expand-from-anchor (high reasoning on finalists)   (auto)
☐ 3. Present the ranked shortlist
```

**Review — the one checkpoint.** After `build_eval_inputs` writes `plan.json`, STOP and show the
user, grouped for a 10-second read:
- the **core** must-haves (the domain differentiators that GATE the shortlist) vs the
  **table_stakes** must-haves (generic seniority/leadership — these only RANK), and
- the **target_level** + asymmetric seniority band.

The plan is the highest-leverage artifact — the core must-haves *are* the shortlist gate — so this
is where the human sharpens a niche role ("delivered large hardware" → "delivered large
*fusion/plasma* hardware") or just confirms the domain for a common one. Let the user edit
`plan.json`, then proceed. **Do NOT ask again** — judging + expansion run autonomously to the end.

**JD input.** Supply the role either way — job-posting URLs, pasted JDs, and complex role briefs
all run here:
- **pasted JD / role brief** → write it to `<run>/jd.txt` and pass `--jd-file <run>/jd.txt`.
- **job-posting URL** → pass `--jd-url <url>` instead; `deep_search_loop` fetches it to `<run>/jd.txt`
  via `fetch_jd.py` (stdlib, no spend) before sourcing. Provide exactly one of `--jd-file` /
  `--jd-url`. JS-rendered careers pages come back `thin` with a warning — paste the JD instead.
- **backend** — when the recorded decision says `backend: local`, add `--backend local --db <db>`
  to every `deep_search_loop` invocation: sourcing probes run against the local DuckDB index
  instead of TurboPuffer/Postgres (no `--set-id`, no pinned seniority bands; judging is unchanged).

The first `deep_search_loop` invocation sources and builds the plan, then stops at Review with
`status: awaiting_plan_approval`:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt --run-dir <run> --set-id <set> --created-at <iso> \
  --max-epochs 3 --score-threshold 0.40 --judge codex --reasoning-effort high
# or, from a job-posting URL (no separate fetch step):
#   ... deep_search_loop.py --jd-url "https://job-boards.greenhouse.io/acme/jobs/123" --run-dir <run> ...
```

Review/edit `<run>/epoch0/plan.json`, then resume the autonomous engine. Resume does **not**
rebuild or overwrite the approved plan; it judges free by default, **core-gates** the shortlist,
and expands from your own judged-strong each epoch:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt --run-dir <run> --set-id <set> --created-at <iso> \
  --max-epochs 3 --score-threshold 0.40 --judge codex --reasoning-effort high \
  --plan-approved
```

Writes `<run>/shortlist/ground_truth_ranked.json` + `<run>/loop.json` (per-epoch convergence). To
drive Review by hand, run stages 1–2 below up to `build_eval_inputs`, review the plan, then continue
from the judge. Pass the approved `plan.json` to `judge_consensus --plan` so the **core-gate** fires.

**Why a gate at all (and why the score alone can't replace it).** A blended `jd_score` cannot tell
a role with real candidates from one with none: pedigree + generic leadership inflate wrong-domain
seniors, so the top of a fusion-VP search scores as high as a real distributed-systems search
(both peak ~0.72–0.77). The fix is the **core-gate**, not a score cutoff — and a clean core comes
from Review. Measured (real judged data): **AgentMail distsys MTS → 88 shortlisted (filled)**, gems
on top (Modal/Anyscale/NVIDIA AI-infra); **Realta fusion VP → 12 → 7 (broad "hardware" core, crypto/
HFT/consumer false-positives excluded) → 2 (Review-sharpened fusion core)** — a clean, self-limiting
give-up driven entirely by core-trait sharpness.

## The core finding this skill is built on (read once)

The existing `search_network_pipeline` is **not** recall-limited. A single broad probe at full
depth contained **31/31** ground-truth people — but a single query's ranking scatters them
(rank 5 → 5089), so a top-50 cap keeps only ~16%. **Diverse probes fix this:** a probe tailored
to "schedulers" top-ranks the scheduler people, an "inference" probe top-ranks the inference
people, etc. Measured convergence (recall vs a 31-person judged ground truth):

| sourcing | pool | GT recall |
| --- | --- | --- |
| 1 naive probe, keep top-50 | 50 | 16% |
| wide search (~18 probes), keep top-40 | 509 | 65% |
| wide search, keep top-80 | 954 | 100% |

So: **wide search for recall, judge for precision.** Don't tighten retrieval to get precision —
that's what drops good candidates. Keep recall high and let the judges gate.

## Flow (every step is a callable primitive — Codex / any harness runs it identically)

Legend: 🆕 = new `deep_search/` primitive · ✅ = existing primitive. No step relies on a harness
improvising. With the free `codex_judge`, a whole run can be **$0 OpenAI**.

1. **Robust source** 🆕 (read-only retrieval; only LLM cost is cheap `gpt-4o` decompose/expand).
   A single `decompose_jd → run_wide_search` round is **flaky** — the LLM seed set varies, so GT
   recall swings (measured 0.87–0.97 across trials). `robust_source` removes that variance by
   unioning several independent rounds (each a fresh decompose with a rotated emphasis) until
   coverage saturates. **Measured (AgentMail, 3 trials): single round 0.87–0.97; 2-round union
   min 0.968 / mean 0.978 (always ≥0.95); 3 independent runs union to 1.00.**
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/deep_search/robust_source.py \
     --jd-file <run>/jd.txt --run-dir <run> --set-id <set> --n 16 --keep 200 --max-rounds 3
   ```
   Writes `<run>/union.jsonl`. (It chains `decompose_jd` + `run_wide_search` internally — those stay
   callable on their own for a single quick pass.) **Recall is fixed HERE, in sourcing — not by
   loosening the judge.**

2. **Bridge the union into the judge's contract** 🆕 (1 cheap LLM call). The judge reads a
   profile-search run dir (`plan.json` + `candidate_frontier.json/jsonl` + `probe_summaries.json` →
   the already-on-disk `profiles.jsonl.gz`). `build_eval_inputs` extracts must/nice traits **and a
   `target_level`** from the JD (so the judge's seniority gate is asymmetric around the role: in-band
   = target and one level below = *step up*; too_senior = one level above or higher = *won't step
   down*; too_junior = two+ below). Defaults to `senior_ic`. It also **tags each must-have
   `core` vs `table_stakes`** — `core` = the 1–3 domain differentiators that make THIS role hard
   (the shortlist GATE); `table_stakes` = generic seniority/leadership/stage (RANK only). This is the
   plan the human approves at **Review**. Rewrites the run into the contract — no recompute:
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/deep_search/build_eval_inputs.py \
     --run-dir <run> --jd-file <run>/jd.txt --set-name "<set>" --created-at <iso>
     # loop epochs reuse the approved plan (no re-extract): --plan <run>/epoch0/plan.json
   ```

   *Two-phase judging is the default.* The loop runs **phase 1: triage** (`triage_candidates`,
   cheap batched `gpt-4.1-mini`, conservative keep/maybe pass — only clear misses drop) over each
   epoch's frontier, then **phase 2: the judge** over the survivors. Rationale: the codex judge is
   token-free but not time-free (~40s/candidate — a 2k union is hours), and the gpt judge is paid
   per candidate; triage cuts the judged pool several-fold for cents. It is mildly lossy
   (measured on an earlier pass: 2 reachable GT dropped) — pass `--no-triage` for a
   maximum-fidelity run where wall-clock/cost don't matter. Pick the phase-2 engine with
   `--judge codex|gpt` (or the `POWERPACKS_DEEP_JUDGE` env preference): codex = free/slower,
   gpt = paid `gpt-5.4` on the flex tier, fast. **CLI agent engines (codex/claude) are phase-2
   judges only — they never do the thousands→hundreds bulk cut, and there is no fallback that
   hands them one:** triage failure fails the run loud, and `--no-triage` over a frontier with
   more than ~300 unjudged candidates requires `--judge gpt`.

3. **Judge (the precision stage)** ✅ `evaluate_profile_candidates` — the canonical bar-raiser
   rubric with the IC seniority hard-gates (default `gpt-5.4`). **Default to a CROSS-VENDOR panel:**
   one `gpt-5.4` judge at `--reasoning-effort low` (measured: ~as good as `high` here, far cheaper)
   **+ one Claude judge on the same rubric.** Cross-vendor agreement is the real confidence signal —
   when both vendors say strong, surface it; when they split, that's the human-review pile. Collect
   each pass's verdicts into a `judges/` dir; `judge_consensus` ingests the
   `evaluate_profile_candidates` raw format directly (maps `candidate_id`/`jd_score`, derives
   `in_band` from `seniority_fit`) alongside native Claude-judge JSONL.
   - **FREE / portable judge:** `deep_search/codex_judge.py` spawns `codex exec` subprocesses, reusing
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

4. **Consensus + rank** 🆕 (the **core-gate** lives here):
   ```bash
   uv run --project . python packs/search/primitives/deep_search/judge_consensus.py \
     --judges-dir <run>/judges --union <run>/union.jsonl --out-dir <run>/shortlist \
     --plan <run>/plan.json --score-threshold 0.40 --min-inband-votes 2   # single judge: 1
   ```
   → `shortlist/ground_truth_ranked.json` (stack-ranked). **With `--plan`, the shortlist is
   CORE-GATED:** membership = majority in-band AND the candidate genuinely DOES ≥1 `core` domain
   capability (`experienced`/`doing_now`; `capable` does NOT count) AND clears the score floor.
   `table_stakes` traits only rank — so a strong-but-wrong-domain senior is excluded no matter how
   high their blended score (that is what collapses the give-up case). Without core tags it falls
   back to the score gate. Reads the per-trait statuses the judge already emits — rubric untouched.
   **Present in two layers:** the core-gated entries with mean ≥ **~0.55** are the SENDABLE list
   (measured on the AgentMail rerun: the 0.40–0.55 core-gated tail was padding — retail/comms SREs
   a hiring manager would not move on); everything else in `consensus.json` (mean ≥0.40 + in-band,
   no core-gate) is the BENCH — name it to the user as minable, don't mix it into the sendable list.
   Then ✅
   `export_candidate_shortlist` for the sendable CSV. **Measured (AgentMail JD):** single judge
   → recall 48% / p@25 0.36; 3-judge (2,2) → 52% / 0.44; a `(2,1)` gate (majority in-band +
   ≥1 not-out) Pareto-beats it (64% / 0.48) by rescuing borderline candidates one strict judge
   cut — but per the anti-local-maxima rule it stays NON-default until validated on a 2nd JD.

5. **Expand-from-anchor — the core Phase-2 loop (NOT optional)** 🆕. This is the hill-climb engine,
   and `deep_search_loop --plan-approved` runs it automatically every epoch after the first judge: take your *own*
   judged-strong picks (a DIVERSE set — `deep_search_loop` dedups anchors by company so you don't
   echo-chamber one archetype), build "more like this" seeds from their profiles, re-source, and
   judge **only the new** candidates; loop until an epoch adds no new strong (converged) or
   `--max-epochs`. The JD is a lossy proxy — a proven-strong profile is the highest-signal query
   for the adjacent people the JD wording never names. NEVER seed from the eval ground truth (that's
   looking up the answers; only matters for the recall *metric*). Self-limiting: ~0 strong → no
   anchors → loop ends (correct give-up). Manual form:
   ```bash
   uv run --project . python packs/search/primitives/deep_search/expand_from_anchor.py \
     --anchors <run>/shortlist/ground_truth_ranked.json --top-k 6 --out <run>/anchor_seeds.json
   uv run --env-file .env --project . python packs/search/primitives/deep_search/run_wide_search.py \
     --seeds <run>/anchor_seeds.json --run-dir <run>/anchor --limit 200
   ```

6. **Measure convergence (epochs)** 🆕. Score any run against a trusted ground-truth set:
   ```bash
   uv run --project . python packs/search/primitives/deep_search/score_ground_truth_gaps.py \
     --ground-truth <run>/ground_truth/ground_truth_ranked.json \
     --epoch-candidates <epoch>/candidates.json \
     --epoch-dir <epoch> --epoch-label epoch-NN --convergence-csv <run>/convergence.csv
   ```
   `gaps.json` = recall@k / precision@k / missed-GT; `convergence.csv` = one row per epoch.

**Ground truth** (the yardstick for hill-climbing) is built once by running the sourcing+judge
the *thorough* way — many hand-diverse seeds + the full judge panel — independently of the cheap
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

## Artifacts (gitignored under `.powerpacks/deep-search/<jd-slug>/`)

`BRIEF.md` · `probes/<family>/…` · `candidates_union.jsonl` · `judges/*.jsonl` ·
`shortlist/{consensus.json,ground_truth_ranked.json}` · `epochs/<epoch>/{config,candidates,gaps}.json` ·
`convergence.csv`. Candidate PII stays gitignored; surface the shortlist to the user.

## Default recipe (fully primitive-driven; robust ≥0.95 sourcing; $0-OpenAI option)

`robust_source` (multi-round `decompose_jd`+`run_wide_search` union → non-flaky ≥0.95 recall) →
`build_eval_inputs` (tags must-haves core/table_stakes) → **Review (human approves the plan)** →
`codex_judge` (FREE; or paid `evaluate_profile_candidates`, ×N for a cross-vendor panel) →
`judge_consensus --plan <plan> --score-threshold ~0.40` (**core-gate**) → `export_candidate_shortlist`;
`expand_from_anchor` each epoch (auto in `deep_search_loop`); `score_ground_truth_gaps` to track epochs.
Every step is a CLI any harness can call — nothing depends on an agent improvising.

**Validated on the AgentMail JD:** sourcing min 0.968 / mean 0.978 recall across 3 independent
trials (single-round was a flaky 0.87–0.97); free codex judge at cutoff 0.30 recovers ~all sourced
GT; cross-vendor union cleans the top (p@10 0.40→0.50). Recall is fixed in **sourcing** (redundant
rounds), precision via the **judge** + **score-threshold** — not by loosening the rubric.

## Primitives

New (`packs/search/primitives/deep_search/`):
- `deep_search_loop.py` 🆕 — **the gate/resume orchestrator**: first run sources + builds the plan and
  stops at `awaiting_plan_approval`; rerun with `--plan-approved` or `--approved-plan` to judge →
  expand-from-anchor → re-judge, converge-capped. Incremental judging is staged through a separate
  new-candidate frontier so canonical frontier artifacts stay intact. Self-limiting give-up when
  there are no strong anchors. Child primitive failures and missing required artifacts fail loudly
  with structured JSON instead of reporting false convergence.
- `robust_source.py` 🆕 — **non-flaky sourcing**: unions independent `decompose_jd`+`run_wide_search`
  rounds (rotated emphasis) until coverage saturates. Turns flaky 0.87–0.97 single-round recall
  into a tight min-0.968 / mean-0.978.
- `decompose_jd.py` 🆕 — JD → N diverse work-described seeds (1 LLM call).
- `run_wide_search.py` 🆕 — runs the seed set through prepare→diversify→run, emits the union.
- `diversify_probe_bm25.py` 🆕 — drop shared/homogeneous BM25 lead terms across the probe set.
- `build_eval_inputs.py` 🆕 — union → `plan.json` + canonical `candidate_frontier.json` +
  streaming `candidate_frontier.jsonl` + `probe_summaries.json` (bridges the wide search run into the
  canonical judge/export contract; 1 LLM call, or `--plan` reuse without a new `--created-at`).
- `triage_candidates.py` 🆕 — cheap-model conservative tier-1 filter over the frontier.
- `codex_judge.py` 🆕 — **free, portable** judge: spawns `codex exec` subprocesses, reusing the
  canonical rubric + deterministic scorer from `evaluate_profile_candidates` (same bar, $0 engine
  via ChatGPT-subscription auth). Prompt/profile content is passed via stdin rather than argv.
  Drop-in for the paid gpt-5.4 judge; same raw output shape.
- `expand_from_anchor.py` 🆕 — judged-strong anchors → "more like this" seeds (no LLM).
- `judge_consensus.py` 🆕 — combine judge passes (native, `evaluate_profile_candidates` raw, or
  `codex_judge` raw) → consensus shortlist. **`--plan` core-gates** membership on the plan's `core`
  domain must-haves (genuinely doing ≥1 at `experienced`+) so wrong-domain seniors are excluded
  regardless of score; `--score-threshold` is the floor/recall dial on the canonical score.
- `score_ground_truth_gaps.py` 🆕 — epoch scoring + convergence vs a ground-truth set.

Existing (reused):
- `search_network_pipeline … prepare --preserve-query-semantic` — keeps the raw query as the
  semantic vector + adds BM25 + structured filters (location/education/company/seniority/
  headcount). Without the flag, expansion rewrites the vector and ~halves recall.
- `search_network_pipeline … run --search-only` — read-only hybrid (BM25+vector) scoped retrieval + Postgres hydrate.
- `llm_filter_candidates` — cheap conservative triage. `evaluate_profile_candidates` — canonical
  judge rubric + IC seniority gates. `export_candidate_shortlist` — sendable shortlist.
