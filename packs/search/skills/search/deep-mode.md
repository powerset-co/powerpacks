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
- 2026-07-09: Plan critic + opt-in micro-sort. plan_critic runs automatically before the Review
  checkpoint (one judge-grade LLM call + deterministic enum checks) and its findings ship in the
  awaiting_plan_approval output — surface them to the user with the plan (validated: catches
  off-enum hire_stage, self-contradictory cutoffs, dropped JD pillars; zero false flags on a good
  plan). micro_sort_shortlist (agentic merge sort ported from network-search-api: 0.1 score bands,
  pack/split/merge, judge evidence as ordering signal, scores never mutated) is available via
  --micro-sort but NON-DEFAULT per the anti-local-maxima rule — measured neutral on the audited
  22-person benchmark (mean audited-top10 rank 4.0 -> 4.7); needs a benchmark that can score
  top-band ordering before defaulting on.
- 2026-07-09: Geo-first sourcing. decompose_jd extracts the JD's metro and appends it to ~3/4 of
  the probe seeds (every 4th stays global as the recall hedge); --location overrides, --location
  global disables. Previously NO location was used at all (JD or company) — verified live: an SF
  on-site JD produced 16/16 location-free probes.
- 2026-07-03: Present the shortlist with a ~0.55 SENDABLE CUT (measured on the AgentMail rerun:
  the core-gated tail below ~0.55 was padding); keep the full score-only pool (consensus.json,
  mean >=0.40 + in-band, no core-gate) as the bench to mine. Judge rubric gained research-evidence,
  lead-IC, consultant-enforcement, split-focus, and ambiguous-title rules (evaluate_profile_candidates).
- 2026-07-08: Rename the human checkpoint "GATE 1" -> "Review" in the printed task checklist and
  prose (the plan-approval step). The algorithmic core-gate (which candidates make the shortlist)
  keeps its name. Engine flags are unchanged (--plan-approved / awaiting_plan_approval).
- 2026-07-10: Move plan extraction + critic + Review before retrieval. The approved plan now drives
  epoch-0 decomposition. Add versioned recruiter defaults, explicit alternative all-of core groups,
  decision.json backend enforcement, and separate shortlist/sendable/bench outputs. Correct the
  automated judge claim: the loop runs one selected judge; multi-vendor panels remain manual/planned.
- 2026-07-10: Keep generated core groups singleton-by-default for eligibility while scoring all
  must-haves. Only deliberate paths approved at Review change path scoring; every conjunction is a
  Review decision and groups larger than three traits are rejected. Explicit unknown seniority may
  stay in the qualified/anchor recall pool, but never becomes sendable.
-->

> **This is `$search`'s deep mode.** Job-posting URLs, pasted JDs, complex role briefs, "build a
> shortlist", and "more people like <url>" all land here via the recorded Step-1 decision —
> JD→judged-shortlist with a reviewed recruiter plan, selected bar-raiser judge, core-gate, and IC-track-aware
> seniority.

## The engine

Source, judge, and rank candidates for a JD from a Powerset set, the way a sourcing team would. This
is the productized version of the agentic-search method in `packs/search/docs/agentic-search.md`.

## Run it: ONE human gate, then autonomous

Track the run as **native harness tasks (checkboxes)** so progress is visible/resumable. There is
exactly **one human touchpoint — Review, the plan** — and everything after it runs to a finished,
ranked shortlist with no further prompts (judge auto, no spend confirm; expand-from-anchor auto).

```
☐ 1. Extract + critique plan    build_eval_inputs --plan-only → plan_critic
      ──▶ Review: show the plan/defaults, get approval/edits   ← the ONLY human touch
☐ 2. Source → triage → judge → core-gate → expand-from-anchor   (auto)
☐ 3. Present the ranked shortlist
```

**Review — the one checkpoint.** After `build_eval_inputs` writes `plan.json`, the loop also runs
`plan_critic` (advisory, one LLM call) and includes its findings in the awaiting_plan_approval
output — show them WITH the plan; they name missing core pillars and cutoff contradictions, the
top measured error source. Then STOP and show the user, grouped for a 10-second read:
- the **core groups** vs **table_stakes** must-haves: generated defaults are one singleton
  eligibility alternative per core trait, while a deliberate alternative path or multi-trait
  conjunction changes path scoring and must be explicitly approved at Review; reject any group
  larger than three traits, and
- the **nice-to-haves**, **target_level**, hire stage, sourcing location, and resolved recruiter
  defaults (including ranking weights and provenance).

The plan is the highest-leverage artifact. Default singleton groups mean direct evidence for any
one core capability can establish eligibility, but the default score still considers all
must-haves. Satisfying one approved group prevents missing sibling alternatives from forcing
`OUT`; singleton eligibility does not silently discard the other requirements from ranking.
This is where the human sharpens a niche role ("delivered large hardware" → "delivered large
*fusion/plasma* hardware") or deliberately defines an alternative/conjunctive path. When Review
changes the default grouping into a scoring path, change that group's `source` from `default` to
`user` (or `jd` when the JD explicitly defines the alternative). Let the user
edit `plan.json`, then proceed. **Do NOT ask again** — judging + expansion run autonomously to the
end.

Also surface the **sourcing location** at this checkpoint: the recruiter plan extracts the JD's stated
metro (e.g. "San Francisco, CA · on-site" → "San Francisco Bay Area") and geo-constrains ~3 of 4
probes to it — smaller first blast radius — while every 4th probe stays global as the recall hedge
(relocators/remote-friendly hires are real: a measured SF-role GT included Seattle/NY/Bengaluru
people). Edit `plan.json.search_scope.location` to correct it or use `null` for global sourcing;
the approved value is passed to decomposition.

**JD input.** Supply the role either way — job-posting URLs, pasted JDs, and complex role briefs
all run here:
- **pasted JD / role brief** → write it to `<run>/jd.txt` and pass `--jd-file <run>/jd.txt`.
- **job-posting URL** → pass `--jd-url <url>` instead; `deep_search_loop` fetches it to `<run>/jd.txt`
  via `fetch_jd.py` (stdlib, no spend) before sourcing. Provide exactly one of `--jd-file` /
  `--jd-url`. JS-rendered careers pages come back `thin` with a warning — paste the JD instead.
- **backend** — `deep_search_loop` reads `<run>/decision.json` and enforces its backend. When local,
  pass `--db <db>` if the default path is not correct; sourcing uses DuckDB instead of
  TurboPuffer/Postgres (no `--set-id`, no pinned seniority bands; judging is unchanged). An explicit
  `--backend` that conflicts with the recorded decision fails rather than silently changing corpus.

The first `deep_search_loop` invocation builds/validates/critiques the plan and stops at Review with
`status: awaiting_plan_approval`. It does **not** start retrieval:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt --run-dir <run> --set-id <set> --created-at <iso> \
  --max-epochs 3 --score-threshold 0.40 --sendable-threshold 0.55 \
  --judge codex --reasoning-effort high
# If the user supplied recruiter preferences, first write a JSON object matching
# recruiter-preferences.schema.json and add: --preferences <run>/preferences.json
# or, from a job-posting URL (no separate fetch step):
#   ... deep_search_loop.py --jd-url "https://job-boards.greenhouse.io/acme/jobs/123" --run-dir <run> ...
```

Review/edit `<run>/epoch0/plan.json`, then resume the autonomous engine. Resume validates and
content-hash binds that exact plan/JD plus Powerset set ID or local DuckDB identity before reusing
any derived artifact. It uses the plan to generate epoch-0 probes, runs
the selected judge, **core-gates** the shortlist, and expands from judged-strong candidates:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt --run-dir <run> --set-id <set> --created-at <iso> \
  --max-epochs 3 --score-threshold 0.40 --sendable-threshold 0.55 \
  --judge codex --reasoning-effort high \
  --plan-approved
```

Writes `<run>/shortlist/{shortlist_ranked,sendable_ranked,bench_ranked}.json` plus `<run>/loop.json`.
`ground_truth_ranked.json` remains a compatibility alias for `shortlist_ranked.json`; it is not an
independently audited evaluation ground truth. Pass the approved `plan.json` to every manual
`decompose_jd`/`robust_source` and `judge_consensus` call.

Pre-policy deep plans cannot safely resume: they do not encode the current core-group and policy
contract. Start a new run, repeat Review once, and do not carry old retrieval/judge artifacts into it.

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
improvising. `codex_judge` avoids per-candidate OpenAI judge calls, but contract extraction,
critic, decomposition, probe preparation, and default triage still use configured model APIs.

1. **Resolve + review the recruiter plan** (one plan call + one advisory critic call, no retrieval).
   `build_eval_inputs --plan-only` extracts JD-supported core/table-stakes/nice criteria, singleton
   core eligibility alternatives, level/stage/location, and the resolved versioned recruiter
   defaults. Generated singleton groups preserve broad eligibility while default scoring still
   considers every must-have. Deliberate alternative/conjunctive paths must be approved at Review,
   every conjunction must be surfaced there, and no group may contain more than three traits. The
   generated plan must validate before Review; explicit user edits outrank JD inference, which
   outranks defaults.
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/deep_search/build_eval_inputs.py \
     --run-dir <run>/epoch0 --jd-file <run>/jd.txt --created-at <iso> --plan-only
   ```

2. **Robust source from the approved plan** 🆕 (read-only retrieval; sourcing adds `gpt-4o`
   decomposition calls plus the normal probe-preparation model boundary).
   A single `decompose_jd → run_wide_search` round is **flaky** — the LLM seed set varies, so GT
   recall swings (measured 0.87–0.97 across trials). `robust_source` removes that variance by
   unioning several independent rounds (each a fresh decompose with a rotated emphasis) until
   coverage saturates. **Measured (AgentMail, 3 trials): single round 0.87–0.97; 2-round union
   min 0.968 / mean 0.978 (always ≥0.95); 3 independent runs union to 1.00.**
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/deep_search/robust_source.py \
     --jd-file <run>/jd.txt --plan <run>/epoch0/plan.json \
     --run-dir <run>/epoch0 --set-id <set> --n 16 --keep 200 --max-rounds 3
   ```
   Writes `<run>/union.jsonl`. (It chains `decompose_jd` + `run_wide_search` internally — those stay
   callable on their own for a single quick pass.) **Recall is fixed HERE, in sourcing — not by
   loosening the judge.**

   ⚠ **Manual sourcing and the plan binding do not mix.** `deep_search_loop --plan-approved`
   refuses a run dir that has retrieval artifacts but no `plan_binding.json` ("start a new run") —
   the binding is written only when the LOOP performs the sourcing. If you source manually with
   `robust_source` as above, stay manual for the rest of the run (triage → judge → consensus as
   below); to use the loop, let the loop do the sourcing after approval instead.

3. **Bridge the union into the judge's contract** 🆕 (no plan LLM call on resume). The judge reads a
   profile-search run dir (`plan.json` + `candidate_frontier.json/jsonl` + `probe_summaries.json` →
   the already-on-disk `profiles.jsonl.gz`). `build_eval_inputs --plan` reuses the approved plan and
   only writes the candidate frontier/probe handoff — it does not reinterpret requirements:
   ```bash
   uv run --env-file .env --project . python packs/search/primitives/deep_search/build_eval_inputs.py \
     --run-dir <run>/epoch0 --plan <run>/epoch0/plan.json --created-at <iso>
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

4. **Judge (the precision stage)** ✅ `evaluate_profile_candidates` — the canonical bar-raiser
   rubric with IC seniority hard-gates. The automated loop runs **one selected judge**:
   `--judge codex` (default/free/slower) or `--judge gpt` (paid/faster). An independently configured
   panel can still be run manually by placing judge JSONL files in a directory and calling
   `judge_consensus`; automated panel/dissent orchestration is planned, not shipped.
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
     The current **0.40 qualified floor**, **0.55 sendable cut**, and evaluator's **0.70 top-tier
     excellence gate** are provisional AgentMail-calibrated defaults, not universal hiring truths.
     Keep the rubric fixed while re-benchmarking all three across multiple JDs; both shortlist and
     sendable cuts are configurable at execution time.

5. **Consensus + rank** 🆕 (the **core-gate** lives here):
   ```bash
   uv run --project . python packs/search/primitives/deep_search/judge_consensus.py \
     --judges-dir <run>/judges --union <run>/union.jsonl --out-dir <run>/shortlist \
     --plan <run>/epoch0/plan.json --score-threshold 0.40 --sendable-threshold 0.55 \
     --min-inband-votes 1 --min-notout-votes 1
   ```
   → `shortlist/shortlist_ranked.json` (stack-ranked). **With `--plan`, the shortlist is
   CORE-GATED:** membership = in-band + non-OUT + every trait in at least one approved core group at
   `experienced`/`doing_now` + the score floor (`capable` does not satisfy a core requirement).
   Generated groups are singleton eligibility alternatives, while default scoring still considers
   all must-haves. A deliberately reviewed alternative/conjunctive path may instead use path
   scoring. `table_stakes` traits always rank — so a strong-but-wrong-domain senior is excluded no
   matter how high their blended score. Without core tags it falls back to the score gate. Reads the
   per-trait statuses the judge already emits — rubric untouched.
   **Present in two layers:** the core-gated entries with mean ≥ **0.55** are written to
   `sendable_ranked.json`; lower-confidence/in-dispute candidates stay in `bench_ranked.json`
   (measured on the AgentMail rerun: the 0.40–0.55 core-gated tail was padding — retail/comms SREs
   a hiring manager would not move on). These thresholds remain provisional and can be overridden.
   `consensus.json` contains every normalized judged row. A candidate explicitly judged
   `seniority_fit: unknown` may remain qualified and seed anchor expansion for recall, but is never
   sendable and remains visible on the bench. A missing or invalid seniority value is not in-band;
   an `OUT` row never qualifies or seeds expansion. **Measured (AgentMail JD):** single judge
   → recall 48% / p@25 0.36; 3-judge (2,2) → 52% / 0.44; a `(2,1)` gate (majority in-band +
   ≥1 not-out) Pareto-beats it (64% / 0.48) by rescuing borderline candidates one strict judge
   cut — but per the anti-local-maxima rule it stays NON-default until validated on a 2nd JD.

6. **Expand-from-anchor — the core Phase-2 loop (NOT optional)** 🆕. This is the hill-climb engine,
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
     --anchors <run>/shortlist/shortlist_ranked.json --top-k 6 --out <run>/anchor_seeds.json
   uv run --env-file .env --project . python packs/search/primitives/deep_search/run_wide_search.py \
     --seeds <run>/anchor_seeds.json --run-dir <run>/anchor --limit 200
   ```

7. **Measure benchmark convergence (offline)** 🆕. Score any run against a separately audited
   ground-truth set; this differs from operational convergence (an epoch adds no new shortlist rows):
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

`decision.json` · `jd.txt` · `epoch0/{plan.json,plan_critic.json,union.jsonl,probes/…}` ·
`judges/loop.jsonl` · `shortlist/{consensus,shortlist_ranked,sendable_ranked,bench_ranked}.json` ·
`loop.json`. Candidate PII stays gitignored; surface the sendable list and summarize the bench.

## Default recipe (fully primitive-driven; robust ≥0.95 measured sourcing)

`build_eval_inputs --plan-only` (JD criteria + recruiter defaults) → `plan_critic` → **Review** →
`robust_source --plan` (multi-round approved-plan probes) → `build_eval_inputs --plan` → triage →
one selected `codex_judge` or paid `evaluate_profile_candidates` →
`judge_consensus --plan <plan> --score-threshold 0.40` (**non-OUT + core-gate**) → sendable/bench;
`expand_from_anchor` each epoch (auto in `deep_search_loop`); `score_ground_truth_gaps` to track epochs.
Every step is a CLI any harness can call — nothing depends on an agent improvising.

**Validated on the AgentMail JD:** sourcing min 0.968 / mean 0.978 recall across 3 independent
trials (single-round was a flaky 0.87–0.97); free codex judge at cutoff 0.30 recovers ~all sourced
GT; cross-vendor union cleans the top (p@10 0.40→0.50). Recall is fixed in **sourcing** (redundant
rounds), precision via the **judge** + **score-threshold** — not by loosening the rubric.

## Primitives

New (`packs/search/primitives/deep_search/`):
- `deep_search_loop.py` 🆕 — **the gate/resume orchestrator**: first run builds/critiques the plan
  without retrieval and stops at `awaiting_plan_approval`; rerun with `--plan-approved` or
  `--approved-plan` to source from the approved plan → judge →
  expand-from-anchor → re-judge, converge-capped. Incremental judging is staged through a separate
  new-candidate frontier so canonical frontier artifacts stay intact. Self-limiting give-up when
  there are no strong anchors. `plan_binding.json` prevents a changed contract/corpus from reusing
  stale retrieval or verdicts. Child primitive failures and missing required artifacts fail loudly
  with structured JSON instead of reporting false convergence.
- `robust_source.py` 🆕 — **non-flaky sourcing**: unions independent `decompose_jd`+`run_wide_search`
  rounds (rotated emphasis) until coverage saturates. Turns flaky 0.87–0.97 single-round recall
  into a tight min-0.968 / mean-0.978.
- `decompose_jd.py` 🆕 — JD → N diverse work-described seeds (1 LLM call).
- `run_wide_search.py` 🆕 — runs the seed set through prepare→diversify→run, emits the union.
- `diversify_probe_bm25.py` 🆕 — drop shared/homogeneous BM25 lead terms across the probe set.
- `build_eval_inputs.py` 🆕 — `--plan-only` creates the pre-source contract; full mode maps union →
  approved `plan.json` + canonical `candidate_frontier.json` +
  streaming `candidate_frontier.jsonl` + `probe_summaries.json` (bridges the wide search run into the
  canonical judge/export contract; 1 LLM call, or `--plan` reuse without a new `--created-at`).
- `triage_candidates.py` 🆕 — cheap-model conservative tier-1 filter over the frontier.
- `codex_judge.py` 🆕 — **free, portable** judge: spawns `codex exec` subprocesses, reusing the
  canonical rubric + deterministic scorer from `evaluate_profile_candidates` (same bar, $0 engine
  via ChatGPT-subscription auth). Prompt/profile content is passed via stdin rather than argv.
  Drop-in for the paid gpt-5.4 judge; same raw output shape.
- `expand_from_anchor.py` 🆕 — judged-strong anchors → "more like this" seeds (no LLM).
- `judge_consensus.py` 🆕 — combine judge passes → consensus/shortlist/sendable/bench.
  **`--plan` core-gates** membership on one complete approved group and never lets a score threshold
  override a judge `OUT`; generated singleton groups affect eligibility while default scoring keeps
  all must-haves. `--score-threshold` and `--sendable-threshold` are provisional configurable cuts.
- `score_ground_truth_gaps.py` 🆕 — epoch scoring + convergence vs a ground-truth set.

Existing (reused):
- `search_network_pipeline … prepare --preserve-query-semantic` — keeps the raw query as the
  semantic vector + adds BM25 + structured filters (location/education/company/seniority/
  headcount). Without the flag, expansion rewrites the vector and ~halves recall.
- `search_network_pipeline … run --search-only` — read-only hybrid (BM25+vector) scoped retrieval + Postgres hydrate.
- `llm_filter_candidates` — cheap conservative triage. `evaluate_profile_candidates` — canonical
  judge rubric + IC seniority gates. `export_candidate_shortlist` — legacy standalone CSV exporter;
  it is not wired into the automatic deep loop.
