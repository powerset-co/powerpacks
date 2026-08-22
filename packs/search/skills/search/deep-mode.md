# `$search` deep mode

Use this mode when the recorded Step-1 decision is `surface: people` and
`depth: deep`: a job-posting URL, pasted JD, detailed role brief, explicit deep
search, or a request to build a shortlist.

The default is the result-driven loop validated in the Search v2 Marimo
harness. It searches one broad candidate population at a time through the
ordinary `search_network_pipeline.py`, reviews the reranked top 50, records the
human diagnosis, and proposes one next move. It stops after at most four ponds.
There is no pool-reading judge and scores never decide candidate quality.

## Checklist

Track these as native harness tasks:

```
☐ 1. Prepare the reviewed plan and initial queries
      ──▶ Review: show Core, Nice-to-have, Filters, and the one or two queries
☐ 2. Compile and review the first pond payload
☐ 3. Run the pond and present the top 50 plus pool statistics
☐ 4. Record the human diagnosis and one next move; repeat up to four ponds
```

The Review before retrieval is the skill's single execution confirmation. Do
not ask again after the user approves the plan and queries. Payload edits and
pond diagnoses are the loop's work, not additional spend confirmations.

## Prepare and confirm

Record `.powerpacks/deep-search/<slug>/decision.json` first. The engine enforces
`surface: people`, `depth: deep`, and the recorded backend. Supply exactly one
of `--jd-file` or `--jd-url`; URL input is fetched once to `<run>/jd.txt`.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt \
  --run-dir <run> \
  --set-id <set> \
  --created-at <iso>
```

The first invocation returns `awaiting_plan_approval` and points to:

- `<run>/epoch0/plan.json` — editable Core, Nice-to-have, Filters, and scope.
- `<run>/queries.json` — one broad query and, only when useful, one distinct
  candidate population.

Show both artifacts. After the user edits or approves them, initialize the
fixed Search v2 artifacts without retrieving candidates:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt \
  --run-dir <run> \
  --set-id <set> \
  --created-at <iso> \
  --plan-approved
```

This writes `<run>/results.json` and `<run>/manifest.json` using the exact
`lab.search-v2.v3` and `lab.search-v2.manifest.v3` schemas consumed by Marimo.
The files are overwritten in place throughout the loop; `decision.json` remains
the route contract.

## Run one pond

The current query is `pending_query` in `results.json`. It can be edited before
compilation:

```bash
uv run --project . python \
  packs/search/primitives/deep_search/simple_deep_search.py set-query \
  --run-dir <run> --query '<one clean candidate population>'
```

Compile the query through the normal parallel extractors. Retrieval is capped
at 1,000 so downstream reranking, not query padding, owns precision.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/simple_deep_search.py compile-pond \
  --run-dir <run>
```

Review `<run>/ponds/pond-NN/payload.json` before execution. Edit only the
concrete controls the harness exposes:

- keep/drop individual role-keyword chips;
- add/remove seniority bands in response to the observed pond size;
- drop or widen location fields;
- edit traits, including `temporal: current|past|all`;
- add named rerank exclusions such as chip or mechanical design.

The compiler applies the three initial recruiter patterns and logs every one in
`pattern_default_edits`: prune keyword fan-out, retune seniority for the role,
and drop structured hard filters that duplicate traits. They remain editable.

After editing the payload file, mark that exact file reviewed:

```bash
uv run --project . python \
  packs/search/primitives/deep_search/simple_deep_search.py review-payload \
  --run-dir <run> \
  --rerank-exclusion '<named specialty to penalize>'
```

Then execute it. Add `--backend local --db <db>` to the compile and run commands
when `decision.json` selected local search.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/simple_deep_search.py run-pond \
  --run-dir <run>
```

The iteration record contains the query/payload snapshot, `edit_delta`,
`pattern_default_edits`, top-50 rows, result count, cost, and deterministic pool
statistics: five score bands, level mix, geography mix, and top companies.
Score bands are display-only. Never call someone strong or stop from a score.

## Diagnose and move once

Present the top 50 and pool statistics. The human chooses the diagnosis. Use
choice 1 to accept the displayed suggestion, choice 2 with `--diagnosis` to
override it, or choice 3 to stop. Choices 1 and 2 make one Luna-medium next-move
call; the raw response is checkpointed before parsing.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/simple_deep_search.py decide \
  --run-dir <run> --choice 2 --diagnosis wrong_location \
  --note '<human observation>'
```

The action taxonomy is `stop`, `ranking_fix`, `refine_current_pond`,
`add_adjacent_pond`, `widen_geography`, or `corpus_sparse`. A `ranking_fix`
reuses the existing retrieved pond and lets the human edit rerank exclusions;
it does not launch a new search. Other search actions create one editable
`pending_query`. Repeat compile -> review -> run -> diagnose, stopping honestly
at `corpus_sparse` or after the fourth pond.

Each run remains directly reviewable in Marimo because every epoch appends one
iteration to the same `results.json`, including the input edit and result delta.

## Exhaustive mode (opt-in)

Use `--mode exhaustive` only when the user explicitly requests the legacy
robust-source, triage, judge/consensus, and anchor-expansion engine. Old
exhaustive artifacts remain readable; never mix modes in one run directory.
