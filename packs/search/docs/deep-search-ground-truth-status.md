# Deep-search benchmark findings

> **Dated benchmark evidence, not a product contract or current price quote.**
> These results came from the June 2026 AgentMail distributed-systems benchmark
> on one Powerset corpus. Current behavior is documented in the
> [`$search` architecture](search-architecture.md).

This page preserves the durable findings from the original experiment without
the session log, candidate identities, superseded plans, or implementation
punch list. Candidate-level artifacts remain gitignored under
`.powerpacks/deep-search/`.

## Question

Could a recruiter-style search recover a trustworthy, high-recall candidate
pool from one known corpus, then use evidence-based evaluation to create a
precise shortlist?

The benchmark used a San Francisco individual-contributor distributed-systems
role. The strongest evidence areas included scheduling and control planes,
traffic and routing, inference infrastructure, and observability or performance
work.

## Method

1. Run several independent, set-scoped hybrid retrieval probes aimed at
   different candidate archetypes.
2. Hydrate and deduplicate the union while retaining probe provenance.
3. Have three independent benchmark judges evaluate every candidate against the
   same rubric.
4. Treat majority in-band and majority non-OUT as the benchmark label.
5. Replay cheaper or narrower sourcing configurations against that independently
   judged set.

The three-judge panel was how this benchmark produced labels. It is not the
current automatic product default, which runs one selected judge.

## Results

The first ground-truth build produced:

| Measure | Result |
| --- | ---: |
| Probe families | 5 |
| Unique sourced candidates | 79 |
| Consensus-strong candidates | 31 |
| Unanimous candidates at the top | 10 of 10 |

The retrieval-depth experiment showed why one broad query was insufficient:

| Sourcing configuration | Candidate pool | Recall of the 31-person benchmark |
| --- | ---: | ---: |
| One broad probe, keep 50 | 50 | 16% |
| About 18 probes, keep 40 per probe | 509 | 65% |
| About 18 probes, keep 80 per probe | 954 | 100% |
| Targeted expansion with mixed depth | 901 | 97% |

Independent decomposition rounds reduced seed variance. Across three trials,
single-round recall ranged from `0.871` to `0.968`; multi-round unions had a
minimum recall of `0.968`, a mean of `0.978`, and a combined union of `1.000`.
Those figures measure reachability inside this corpus, not universal recruiter
quality.

A later judge-threshold replay found that sourcing was not the only relevant
lever. On a 68-person ground-truth-enriched pool, lowering the canonical score
cutoff from roughly `0.50` to `0.40` recovered about `0.88` recall while
admitting four non-benchmark candidates. This result motivated a configurable
shortlist floor, but one JD cannot establish a universal threshold.

## Durable conclusions

- Use several semantically distinct probes rather than one very large query.
- Preserve the intended meaning of each probe through expansion and retrieval.
- Build recall in sourcing; use evidence judgment and deterministic gates for
  precision.
- Keep each probe and run bound to its exact plan and corpus identity.
- Expand from strong, diverse candidates at the person level rather than
  extrapolating from shallow probe-family yield.
- Measure every stage separately: sourced, triaged, judged, qualified, and
  sendable.

These conclusions shaped the shipped contract -> critic -> Review -> source ->
judge -> gate -> expand -> converge loop. The retired slice-planning and V1 task
harness did not produce these guarantees and are not part of current search.

## Limits

This benchmark does **not** prove:

- calibrated quality across different job families, levels, or locations;
- parity between Powerset and local DuckDB on equivalent corpora;
- that a three-judge panel runs automatically in the current product;
- that historical thresholds remain calibrated after scoring changes; or
- current latency or cost.

Historical spend notes separated retrieval from inference: TurboPuffer retrieval
and Postgres hydration were read-only, one subscription-backed Codex judging
round reported no incremental OpenAI API charge, and multi-round decomposition
was estimated at roughly `$1-2`. These are experiment notes, not a quote for a
current deep run.

Cross-JD calibration, backend parity, automated panel execution, and stable
cost reporting remain evaluation work. The current shipped/planned boundary and
threshold caveats live in the [canonical architecture](search-architecture.md#shipped-versus-planned).
