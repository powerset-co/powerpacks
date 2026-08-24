# `$search` deep mode

Use this mode when the recorded Step-1 decision is `surface: people` and
`depth: deep`: a job-posting URL, pasted JD, detailed role brief, explicit deep
search, or a request to build a shortlist.

The default is the result-driven loop validated in the search-harness Marimo
harness. It searches one broad candidate population at a time through the
ordinary `search_network_pipeline.py`, reviews the reranked top 50, records the
diagnosis, and proposes one next move. After plan approval the skill runs the
ponds autonomously and stops after at most four ponds.
There is no pool-reading judge and scores never decide candidate quality.

## Checklist

Track these as native harness tasks:

```
☐ 1. Prepare the reviewed plan and initial queries
      ──▶ Review: show Core, Nice-to-have, Filters, and the one or two queries
☐ 2. Compile and review the first pond payload
☐ 3. Run the pond and present the top 50 plus pool statistics
☐ 4. Record one diagnosis and next move; repeat autonomously up to four ponds
```

The Review before retrieval is the skill's single execution confirmation. Do
not ask again after the user approves the plan and queries. Payload edits and
pond diagnoses are autonomous loop work, not additional spend confirmations.

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

- `<run>/epoch0/plan.json` — editable Core, Nice-to-have, Filters, scope,
  JD-quoted candidate populations/ranking boosts, and any posted compensation band.
- `<run>/queries.json` — one broad query and, only when useful, one distinct
  candidate population.

Show both artifacts. After the user edits or approves them, initialize the
fixed search-harness artifacts without retrieving candidates:

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
`search-harness.v1` and `search-harness.manifest.v1` schemas consumed by Marimo.
The files are overwritten in place throughout the loop; `decision.json` remains
the route contract.

## Run one pond

The current query is `pending_query` in `results.json`. It can be edited before
compilation:

```bash
uv run --project . python \
  packs/search/primitives/deep_search/search_harness.py set-query \
  --run-dir <run> --query '<one clean candidate population>'
```

Compile the query through the normal parallel extractors. Retrieval is capped
at 1,000 so downstream reranking, not query padding, owns precision.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/search_harness.py compile-pond \
  --run-dir <run>
```

Inspect `<run>/ponds/pond-NN/payload.json` before execution without pausing for
another approval. Any autonomous edit must use only the concrete controls the
harness exposes:

- keep/drop individual role-keyword chips;
- add/remove seniority bands in response to the observed pond size;
- drop or widen location fields;
- edit traits, including `temporal: current|past|all`;
- add named rerank exclusions such as chip or mechanical design.

One Terra-medium pass proposes the three initial recruiter patterns, using the
JD brief plus similar prior `pattern_default_edits` and human payload edits:
prune keyword fan-out, retune seniority for the role and prior pond size, and
drop structured hard filters that duplicate traits. Every proposal includes a
one-line reason in `pattern_default_edits` and remains editable. The prior
deterministic table runs only if that call or response fails.

After editing the payload file, mark that exact file reviewed:

```bash
uv run --project . python \
  packs/search/primitives/deep_search/search_harness.py review-payload \
  --run-dir <run> \
  --rerank-exclusion '<named specialty to penalize>'
```

Then execute it. Add `--backend local --db <db>` to the compile and run commands
when `decision.json` selected local search.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/search_harness.py run-pond \
  --run-dir <run>
```

The iteration record contains the query/payload snapshot, `edit_delta`,
`pattern_default_edits`, the proposed-versus-human `human_edit_delta`, top-50
rows, result count, cost, and deterministic pool statistics: five score bands,
level mix, geography mix, and top companies. RapidAPI company context is
cache-first: the hiring company is resolved once, and review rows show current
company headcount, latest funding round, company-size move, and the display-only
`in-band` / `promising step-up` / `junior-could-grow` / `wrong-timing` /
`flag-relationship` / `too-senior` / `unhireable` label. The annotator also receives any posted
compensation band and assigns a separate strong / neutral / weak employer
pedigree prior for the role family. Human pedigree overrides saved in Marimo
become retrievable precedents. Missing company matches stay unknown. Score
bands, move labels, and pedigree priors never alter rank or stop the loop.
`results.json.summary` deduplicates candidates across ponds into send-worthy,
chat-worthy, wrong-timing relationship, and passed groups, merging every saved
run of the same JD. Each row keeps the rerank score, level, timing, pedigree,
one-line reason, and finding run; the pond chain and total recorded cost close the summary. Marimo renders this block before the
detailed editable timeline.
The final step presents `<run>/shortlist.csv` and launches that run fully expanded with `uv run --project . python -m packs.search.primitives.deep_search.results_web --run-dir <run> --open`; use `--root .powerpacks/deep-search` only to browse summarized history.

## Diagnose and move once

After each pond, make one Luna-medium call that returns the diagnosis, action,
next query, grounded source, and rationale. It considers JD-quoted candidate
populations before inventing a pond, and retrieves Jake seeds and human-confirmed
decisions; agent-only history is excluded until a human reviews it in Marimo.
The raw response is checkpointed before parsing.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/search_harness.py decide \
  --run-dir <run> --autonomous
```

The action taxonomy is `stop`, `ranking_fix`, `refine_current_pond`,
`add_adjacent_pond`, `widen_geography`, or `corpus_sparse`. A `ranking_fix`
reuses the existing retrieved pond and permits rerank-exclusion edits;
it does not launch a new search. Other search actions create one editable
`pending_query`. `proposal_delta` records the proposed diagnosis/action/query;
`human_override` stays null until Marimo review. A later confirmation or full
action/query override becomes precedent.
Repeat compile -> review -> run -> diagnose, stopping honestly
at `corpus_sparse` or after the fourth pond.

Each run remains directly reviewable in Marimo because every epoch appends one
iteration to the same `results.json`, including the input edit and result delta.

## Exhaustive mode (opt-in)

Use `--mode exhaustive` only when the user explicitly requests the legacy
robust-source, triage, judge/consensus, and anchor-expansion engine. Old
exhaustive artifacts remain readable; never mix modes in one run directory.
