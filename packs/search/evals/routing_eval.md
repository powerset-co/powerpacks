# Routing eval — baseline for the `$search` collapse

_Created: 2026-06-30._

_Changelog:_
- _2026-06-30: initial. Deterministic `route_query.classify` + 48-case labeled fixture; recorded baseline._

## Why this exists

The search consolidation folds `$search-network` into a `$search` that routes deep queries to
`$recruit`. We had evals for the **judge/shortlist quality** (recruit vs ground truth) but **none
for the routing decision** ("does this query go to fast retrieval / recruit / company / sql /
contacts?"). Without a routing eval, "`$search` picks the right strategy" is an unmeasured claim.
This is that eval — pure string rules, no LLM, no spend, CI-runnable.

## What's measured

`packs/search/primitives/route_query/route_query.py` — `classify(query) -> Decision(route, rule,
subroute)` — encodes **today's heuristic** (the AGENTS.md skill-routing prose + the search-network
SKILL's Profile/Local/TurboPuffer rules) as ordered, first-match-wins rules. This same classifier
is what Stage 3 wires into `$search`, so the baseline here is the floor Stage 3 must hold.

Routes: `recruit` (job URL / pasted JD / role brief / shortlist intent / similar-person),
`contacts` (my/set contacts + field filters), `sql` (relational/aggregate/career-shape predicates),
`company` (company lookup/ids/investors/funding/sector), `network` (default fast people retrieval;
`subroute` = local | turbopuffer).

Fixture: `packs/search/evals/routing/cases.json` — 48 labeled queries, ≥6 phrasings per route plus
8 adversarial seam-probes. Genuinely-ambiguous cases carry an `acceptable` alternate; a prediction
is **lenient-correct** if it equals `expected` or is in `acceptable`.

## Baseline (2026-06-30)

Run: `uv run --project . python packs/search/evals/run_routing_eval.py`

| metric | value |
|---|---|
| **strict accuracy** (expected only) | **0.9375** (45/48) |
| **lenient accuracy** (expected ∪ acceptable) | **0.9792** (47/48) |
| subroute accuracy (network local/TP) | 1.0000 (2/2) |
| per-route recall | recruit 0.89, contacts 1.0, sql 1.0, company 0.78, network 1.0 |

### Confusion (rows=expected, cols=predicted)
```
expected\pred    recru    conta      sql    compa    netwo
recruit           8        0        0        0        1     (rec-fits-jd -> network, acceptable)
contacts          0        7        0        0        0
sql               0        0        9        0        0
company           1        0        0        7        1     (shortlist-companies -> recruit; investors-in-ramp -> network, acceptable)
network           0        0        0        0       14
```

### The one documented seam (baseline miss, lenient too)
- `adv-shortlist-companies`: *"shortlist the fintech companies that raised a Series B"* →
  predicted `recruit` (the `shortlist` recruit verb fires), expected `company` (the subject is
  companies). A genuine intent collision on a rare phrasing. Left as a known confusion so the
  baseline is honest rather than tuned-to-fixture. Stage 3 may add a "company-subject beats a
  generic recruit verb when there is no people noun" guard and raise the baseline.

### Genuinely-ambiguous cases (resolved via `acceptable`)
- `rec-fits-jd` ("who in my network fits this staff ML JD") — recruit vs network (JD-fit vs fast).
- `co-investors-in-ramp` ("who are the investors in Ramp") — company vs network ("who" tempts people).
- `adv-find-candidates-bare` / `adv-bare-linkedin-url` — under-specified; default to network, recruit acceptable.

## Anti-overfit notes

I authored both the rules and the fixture, so a clean score is partly self-fulfilling. Mitigations:
≥6 varied phrasings per route; 8 adversarial seam-probes that flip the obvious keyword (people
filtered *by a company attribute* → network, not company; `look up` + people noun → network);
only *generalizing* fixes applied ("overlapped at" added alongside "overlapped with" — broadens
real phrasings, doesn't special-case a fixture row); the shortlist-companies seam left unfixed.
`tests/test_routing.py` locks strict ≥ 0.9375 / lenient ≥ 0.9792 as a regression floor and asserts
≥2 cases per route.

## Gate 2 (met)
- [x] Labeled query set (48; ≥6 phrasings/route + adversarial) with expected routes.
- [x] Harness runs → accuracy + confusion matrix; baseline recorded (strict 0.9375, lenient 0.9792).
- [x] CI-runnable + regression floor locked in `tests/test_routing.py` (15 tests).
