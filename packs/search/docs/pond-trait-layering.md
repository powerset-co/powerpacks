# Ponds and traits: re-layering deep search 🎣

Created: 2026-09-02

Change log:
- 2026-09-02 (implementation): 1, 3, 7, 8 landed on `feat/pond-trait-layering`.
  5 and 6 wait on the trait contract redesign — Arthur wants the plan to
  carry N flat person-traits (no must/nice/core-groups split; a language or
  tool is a trait only when producing it is the job).
- 2026-09-02 (review): Arthur approved 1, 3, 5, 6, 7, 8. Skipped 2 (plan
  ranking-boost hint) and dropped 4 (shared trait set at rerank) — with the
  pond-1 rule in 1, the query keeps one capability from the JD's recurring
  work, so the rerank still has a trait to order on; 4 only mattered for
  fully plain ponds. Watch `pool_stats.score_histogram` on those.
- 2026-09-02: First version. Written after Jake's three Sept-2 feedback runs
  (Listen Labs MTS, Pylon MTS API, Listen Labs Research Scientist), his two
  recorded recruiting walkthroughs, and a full read of the engine at 1.25.1
  (see `../primitives/deep_search/README.md`). Amends the L1–L4 layers of
  `reflect-and-search-v2-proposal.md` for the pond-harness era.

## TL;DR

Retrieval should define **who is in the pool**; one search-level trait set
should decide **who is good**. Today the engine does the opposite in both
directions: the pond query invents a candidate trait from company wording
(B2B SaaS, fintech) and pushes it into retrieval, while the plan's extracted
traits reach neither the rerank nor the company-fit panel. The fix is not a
new stage. It is one prompt deletion, one flag the pipeline already has, one
input-contract change to the panel, and a small calibration of the move gate.

Target shape: **N cached population ponds × one extracted trait layer,
applied at rerank and at the panel.**

## 1. What Jake does

Every role in both walkthroughs and all three feedback runs follows the same
procedure:

1. Compress the JD to **occupation + at most one capability + geography**.
   Years, stack, soft skills, portfolio boilerplate are discarded.
2. **Pond 1 is the simplest credible population**: occupation × geo.
   Seniority bands are a pond-size lever, added only when the pond is visibly
   large, removed when the profile is scarce.
3. **A second pond only when it changes the population**: capability-first
   ("designers who can code", "engineers likely to have RL experience"),
   qualification-first ("studied mech/aero/physics"), employer-vocabulary
   title ("product engineer"), feeder career (early consultants / IB
   analysts), transfer environment (private-jet client services for an EA),
   employer set (frontier labs). Never a paraphrase of the same pool.
4. **Geography widens in rings**, and widens before capability relaxes.
5. **Rerank exclusions come from observed noise** (chip / mechanical "design
   engineer"), after the fact.
6. **Candidate judgment is mostly not JD traits.** What he says while
   judging: company quality and talent bar, tenure and jumpiness,
   recruitability (14 years at Google, a founder who raised $386M, staff at
   Databricks, just joined Thinking Machines), seniority versus the role,
   slope, relocation plausibility, product sense inferred from company type.
   JD evidence (fintech for underwriting, RL for Spectral, search for
   Firecrawl, both halves of design+code) is a **bonus ranker**, not a gate.
7. **Bands**: direct evidence / smart at a high-bar org with the capability
   plausible / too senior but worth a calibration call / junior, could step up.
8. **Stop when a pond is productive; distinguish a bad query from a sparse
   corpus.**

His three Sept-2 corrections are the same doctrine applied to the engine:
"Engineers in San Francisco" (Listen MTS), "Backend or full-stack engineers
in the SF Bay Area" (Pylon), and for the Listen research role a second pond
of "people in technical roles at frontier AI labs" with the explicit note
"do not constrain frontier-lab candidates by what they work on; retrieve
broad technical roles at those companies and let reranking judge relevance",
plus academia as a separate adjacent pond.

## 2. What the engine does today

Facts from the code at 1.25.1 (functions named; see the engine README for
the per-stage table).

| Layer | Today |
| --- | --- |
| Pond query | `decompose_jd --dynamic-simple` with the family `pond-1` prompt. The engineering and general prompts contain a Software rule: *"Otherwise infer one recognizable customer industry or product category from the company overview, such as fintech … B2B SaaS, and use that vertical as X"*, *"the customer industry or product category outranks the role's internal technical layer"*, *"For ordinary software roles, X must be the recognizable customer industry or product category"*. This contradicts the same prompt's own defaults ("Default to the plain occupation", "Add one experience only when the plain occupation would retrieve the wrong people"). The prompt receives the JD, title, location, candidate populations, and ≤1 move card — **not `must_have` or `nice_to_have`**. |
| Retrieval | `search_network_pipeline prepare` runs eight extractors on the pond query. Ordering is bm25 over role phrases + kNN over the role extractor's `semantic_query`; hard filters are seniority bands, location, `is_current`, set scope (occupation `role_ids` are hard filters only for founder / C-suite). Capped at 1000. The query's `traits[]` do not shape retrieval ordering. |
| Rank inside the pond | `llm_rerank_candidates` scores every filtered candidate against the pond query's `traits[]` (one call each, gpt-5.6-luna). So "Backend Engineer with fintech experience" ranks by *fintech* first. Scores are not comparable across ponds because each pond has its own rubric. |
| Hidden layer | The company-fit panel (`role_fit`, `craft_and_potential`, `company_taste`, `move_feasibility`, then a decision call) runs on rows with rerank ≥ 0.70. `_fit_input` sends the full JD, `target_level`, a one-line brief, `comp_band`, hiring-company context, and the candidate — **not `must_have`, `core_groups`, or `nice_to_have`**. The role-fit expert re-derives the requirements from the raw JD on every candidate; the MoE calibration session measured that as prompt-only instability. The panel **buckets** (send-worthy / chat-worthy / wrong-timing / passed via a first-rule-wins override) but never reorders: order inside a group is the pond rerank score. |
| Move gate | `send_worthy & move != plausible → chat_worthy`. With no `comp_band` in the JD the move expert returns `unclear` for nearly everyone, so send-worthy is unreachable (Listen RS: 0/78 plausible, 61 unclear, 0 send-worthy). |
| Next pond | `decide` with the family `next-pond` prompt, ≤3 move cards, floors, pool stats. The seed card deck encodes the same customer-industry policy in some chains ("Software Engineer with fintech experience in New York Metropolitan Area", reason "start with the broad software-engineering population in the customer industry"). |
| Plan traits | Extracted once (`build_eval_inputs`: up to 4 core `must_have`, `nice_to_have`, two-thirds `core_groups`, `candidate_populations` with a `ranking-boost` hint that is "always preserved when stated") and then used only to retrieve precedent cards and to build the one-line brief. The full per-trait judge (`evaluate_profile_candidates` + `judge_consensus` core gate) runs only under `--mode exhaustive`. |

The card hypothesis for Pylon was tested and does not hold: with the seed
deck alone, no card clears the ranking floor for the Pylon JD, so nothing was
injected; the fintech wording is what the Software rule dictates with no
card present. The seed deck still needs scrubbing because a fixed prompt
would be re-taught the old policy by "reviewed human guidance".

## 3. The gap in one sentence

The traits live in the wrong places: the invented one is in retrieval, the
extracted ones are nowhere.

## 4. Target layering

```
L1  Pond        population only: occupation × geo × seniority lever, or an employer set,
                a feeder career, a qualification. No candidate-trait clause. Cacheable —
                "software engineers in SF" is the same pool for every search.
L2  Order       llm_rerank over ONE trait set per search: the plan's core traits
                (+ the pond's own capability when a capability-first pond was chosen).
                Same rubric in every pond → comparable scores → one merged ranking.
                Picks the rows that get L3. Cheap (luna, ≈$0.10 per 1000).
L3  Judge       the company-fit panel with an explicit contract:
                  JD half   — role_fit scores must_have / core_groups / nice_to_have
                              with the evidence ladder the exhaustive judge already uses;
                  taste half — craft, company_taste, move_feasibility unchanged.
                Orders inside groups by JD evidence, then L2 score. Buckets as today.
L4  Merge       dedup across ponds; bands; next-pond decision from L3 yield per pond.
```

This is Arthur's "1 retrieval × 1 filter × N reranks" with the one necessary
amendment: retrieval cannot be *only* the filter, because the pool is bigger
than the review budget; something has to order it, and that something is the
shared trait set — never the occupation choice.

## 5. Changes, smallest first

Each item is independently shippable and small. 1–3 fix the reported
defect; 5–6 are the hidden layer; 7–8 are one-condition calibration and
observability fixes. Decided 2026-09-02: ship 1, 3, 5, 6, 7, 8; skip 2; drop 4
(see change log).

1. **Delete the customer-industry mandate** in
   `packs/search/prompts/pond-1.txt` and every `prompts/families/*/pond-1.txt`
   that carries it. Replacement rule: X may come only from explicit
   candidate-background language or the role's recurring work; otherwise the
   plain occupation. The prompt's existing top-level defaults then produce
   Jake's Pond 1 unaided.
2. **Ranking-boost only when the JD ties domain to the candidate.** In the
   plan prompt (`expand_search_request/prompts/trait_generation.txt` via
   `DEEP_PLAN_ADAPTER_PROMPT`), change *"always preserve it when stated"* to
   preserve only when the JD connects prior domain familiarity to candidate
   success. Company blurbs stop becoming ranking hints.
3. **Scrub the seed move cards** in
   `packs/search/policies/search-harness-precedents.json` whose chains carry
   a customer-industry X. Keep the ones that already say "broad pond, judge
   in review".
4. **One trait set per search at L2.** In `search_harness.run_pond`, pass
   `--evaluation-traits-json` (the pipeline and `llm_rerank_candidates`
   already accept it) with the plan's core traits, plus the pond's own
   capability trait when the pond query carries one. Record the trait set on
   the iteration. This makes `final_score` mean the same thing in every pond
   and lets the summary merge honestly.
5. **Explicit contract for the JD half of L3.** `company_context._fit_input`
   passes `traits.must_have` (with tier), `core_groups`, and `nice_to_have`;
   `ROLE_FIT_PROMPT` scores each trait on the existing ladder
   (`doing_now | experienced | capable | foundational | thin | missing | unknown`)
   and returns the label from that, instead of re-reading the JD. This is the
   fix the MoE calibration session arrived at; the rubric and status
   vocabulary already exist in `evaluate_profile_candidates`.
6. **Let L3 order.** Inside each summary group sort by role-fit trait
   coverage (a deterministic score from the per-trait statuses — reuse the
   trait half of `normalize_evaluation`), then `final_score`. The panel stops
   being bucket-only.
7. **Move gate with no comp band.** When `plan.comp_band` is null, an
   `unclear` move does not demote send-worthy, and the summary reports how
   many rows the move gate held back and why. Zero send-worthy must never be
   an input-data artifact reported as a candidate finding.
8. **Persist the Pond-1 card.** `decompose_jd` writes the injected card into
   `queries.raw.json`. Today neither the user nor a reviewer can see what
   steered the first query.

Pond doctrine for the `next-pond` prompts follows from Jake's rules and
needs only wording: `add_adjacent_pond` is legal only for a different
population (capability-first, qualification, feeder career, employer set,
transfer environment); `widen_geography` moves one ring; a paraphrase of the
current pool is never a move. Employer-set ponds compile today through the
company extractor and `resolve_companies` (company-id filter + broad role),
so Jake's frontier-labs pond needs no new retrieval.

## 6. What not to build

- No new stage, state file, run id, or ledger. Every change above lands in
  an existing prompt, flag, or input dict.
- No return of `robust_source`, `triage_candidates`, or anchor expansion by
  default; the exhaustive mode stays opt-in.
- No per-pond trait sets. One trait set per search is what makes scores
  comparable and the merged shortlist coherent.
- No runtime MOE. Calibrate the single panel model with the explicit
  contract first (the stricter-prompt experiment already showed prompt-only
  tuning oscillates).

## 7. Regression cases

Mirror the decision-eval pattern (cases file, deterministic assertions):

- Query generation: Pylon MTS API → `Backend Engineer` (no X); Listen MTS
  Platform → `Software Engineer` (no `B2B SaaS`); a positive control whose
  JD says "prior fintech/payments experience required" → X preserved.
- Plan hints: served industry only in the company blurb → no
  `ranking-boost`; "familiarity with mortgage servicing a plus" → preserved.
- Harness: every pond iteration records the same `evaluation_traits` for a
  run; `final_score` distributions are computed on that set.
- Panel: with `must_have` passed explicitly, the six negatives and six
  accepted positives from the MoE calibration session hold on both the
  conservative prompt and the explicit-contract prompt.
- Move gate: `comp_band: null` + move `unclear` + send-worthy decision →
  stays send-worthy and is counted in the "held by move gate" line only when
  a comp band exists.

## 8. Open questions

1. Should `nice_to_have` enter L2 ordering at a lower weight, or stay
   L3-only? Jake treats domain evidence as a bonus, which argues for L2 at
   low weight.
2. Employer-set ponds: is the company extractor's resolution reliable for a
   list of seven labs, or does the harness need a `company_names` override on
   `set-query`?
3. `FIT_ANNOTATION_LIMIT` is 500 rows per pond at ≈5 calls each. With
   comparable L2 scores, should the panel gate move from a fixed 0.70 to a
   per-search budget?
