# `$search` deep mode

Use this mode when the recorded Step-1 decision is `surface: people` and
`depth: deep`: a job-posting URL, pasted JD, detailed role brief, explicit deep
search, or a request to build a shortlist.

The default is the result-driven loop validated in the search-harness Marimo
harness. It searches one broad candidate population at a time through the
ordinary `search_network_pipeline.py`, runs the company-fit panel on every
candidate scoring at least 0.70 (or at least 0.30 when none clear 0.70), shows
every retrieved row in the viewer, and asks the user one thing: keep going or
done. Diagnosis and the next query
are the model's job, never the user's. An explicit `mode: auto` in
`decision.json` runs the whole loop without the per-pond pause and stops after
at most four ponds. Interactive mode also completes at that point, but an
explicit user request can reopen it for one more pond at a time.
There is no pool-reading judge and scores never decide candidate quality.

## Checklist

Track these as native harness tasks:

```
☐ 1. Prepare the reviewed plan and initial queries
      ──▶ Review: show the query first, then Filters — nothing else
☐ 2. Run the pond and open its results in the viewer
☐ 3. Ask: review in the viewer, leave feedback — another round, or done?
☐ 4. On "another round": model crafts the next query; state it and run
      (auto repeats up to four ponds; explicit user requests remain binding)
☐ 5. Complete
      ──▶ Present shortlist.csv and the final summary line
```

The plan Review before retrieval is the skill's single spend confirmation and
the only approval in the whole flow. The per-pond pause is a continue-or-done
question, not an approval gate. In auto mode there is no per-pond pause;
review happens at the end.

## Prepare and confirm

Record `.powerpacks/deep-search/<slug>/decision.json` first. The engine enforces
`surface: people`, `depth: deep`, and the recorded backend. Supply exactly one
of `--jd-file` or `--jd-url`; URL input is fetched once to `<run>/jd.txt`.
If `fetch_jd` fails, or warns that extraction was thin (JS-rendered page), ask
the user to copy-paste the JD text into the chat — never mention flags or file
paths to them — then write the pasted text to `<run>/jd.txt` yourself and
continue with `--jd-file`.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt \
  --run-dir <run> \
  --set-id <set> \
  --created-at <iso>
```

The first invocation returns `awaiting_plan_approval` and points to:

- `<run>/epoch0/plan.json` — editable traits (1–6 ordered person-traits, each
  quoting the JD), Filters, scope, JD-quoted candidate populations, and any
  posted compensation band.
- `<run>/queries.json` — exactly one broad query (the generator rejects
  more; a second arm exists only if the user edits the file).

Present the review as exactly two lines — the query on top, filters below:

```
- Query: "<the query>"
- Filters: <level, location, in-person/remote, exclusions>
```

Do not print traits, candidate populations, or the compensation
band; they stay in `plan.json` for the user to open on request and keep feeding
the engine unchanged. After the user edits or approves, initialize the fixed
search-harness artifacts without retrieving candidates:

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

Before execution, review the compiled payload yourself and call
`review-payload` — do not pause for the user (the plan approval already covered
spend; pass `--human-reviewed` only when the user actually edited the payload).
Apply only the concrete controls the harness exposes:

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
`pattern_default_edits`, the proposed-versus-human `human_edit_delta`, all rows
scoring at least 0.70 (or at least 0.30 when none clear 0.70), result count, cost,
and deterministic whole-pool statistics: five score bands,
level mix, geography mix, and top companies. RapidAPI company context is
cache-first: the hiring company is resolved once, and review rows carry the
company-fit panel's four labels — role fit (`strong-fit` / `adjacent-fit` /
`promising-step-up` / `junior-could-grow` / `too-senior` / `wrong-role` /
`unclear`), craft and potential and company taste (`strong` / `neutral` /
`weak` / `unclear`), and move feasibility (`plausible` / `comp-stretch` /
`comp-mismatch` / `wrong-timing` / `destination-pull` / `founder-lock-in` /
`unclear`). The panel receives any posted compensation band and the plan's
traits; the role-fit expert scores each trait on the evidence ladder and every
row carries the result as `jd_fit`. Missing company matches stay unknown. The
decision call picks a row's summary group; labels never reorder rows or stop
the loop, and order inside a group is the rerank score.
`results.json.summary` deduplicates candidates across ponds into send-worthy,
chat-worthy, wrong-timing relationship, and passed groups, merging every saved
run of the same JD. Each row keeps the rerank score, level, timing, pedigree,
one-line reason, and finding run; the pond chain and total recorded cost close the summary. Marimo renders this block before the
detailed editable timeline.
Start the viewer right after the FIRST pond completes, and keep it for the
whole run:

```bash
uv run --project . python -m packs.search.primitives.deep_search.results_web \
  --run-dir <run> --open
```

The viewer shows two panels per search: the main results in rerank order
(authoritative, unchanged), and "JD fit (beta)" — the same graded candidates
ordered by `summary.jd_fit_order` (JD-trait coverage, then rerank score), with
each row's JD trait statuses listed as "JD traits (beta)" under its fit labels.
Never print candidate tables, names, or per-candidate labels in the chat — the
viewer is the only candidate-review surface. After each pond, say only: the
pond's query, the result count, the four group counts (send-worthy /
chat-worthy / wrong-timing / passed), and the viewer URL
(tell the user to refresh after later ponds). When the loop stops, mark task 5
complete and present `<run>/shortlist.csv`. Use `--root .powerpacks/deep-search` only to browse
summarized history.

## Continue or done

After each pond, point the user at the viewer and ask exactly one plain
question — for example: "Results are in the viewer — review them and leave
feedback on any candidate with its … button. Want another round of results
(I'll craft a new query from what came back), or are you done?" Never mention
diagnoses, choice numbers, or the action taxonomy to the user.

- **Another round** → run the model's own diagnosis/move call; then state the
  new query in one line and run the next pond:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/search_harness.py decide \
  --run-dir <run> --choice 2
```

This command also reopens a run the model previously stopped, then produces
one more editable query/payload/run round. An explicit user request for another
round cannot be converted back into a model stop.

- **Done** (or the user is happy) → stop and complete:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/search_harness.py decide \
  --run-dir <run> --choice 3
```

Auto mode makes the same autonomous call after every pond without pausing:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/search_harness.py decide \
  --run-dir <run> --autonomous
```

In either mode, the move considers JD-quoted candidate populations before
inventing a pond and retrieves reviewed seed precedents and human-confirmed decisions;
agent-only history is excluded until a human reviews it in Marimo. The raw
response is checkpointed before parsing. The action taxonomy is `stop`, `ranking_fix`, `refine_current_pond`,
`add_adjacent_pond`, `widen_geography`, or `corpus_sparse`. A `ranking_fix`
reuses the existing retrieved pond and permits rerank-exclusion edits;
it does not launch a new search. Other search actions create one editable
`pending_query`. `proposal_delta` records the proposed diagnosis/action/query;
`human_override` records the user's continue-or-stop choice; the model's own moves stay
unreviewed until Marimo, where a confirmation or full action/query override
becomes precedent.
Repeat compile -> review -> run -> continue-or-done, stopping honestly
at `corpus_sparse` or after the fourth pond.

Each run remains directly reviewable in Marimo because every epoch appends one
iteration to the same `results.json`, including the input edit and result delta.

User edit & feedback capture from the `$search` SKILL applies here too: log each
user-driven query/payload/pond edit and each result comment with
`search_feedback.py log --run-dir <run> --kind pond_edit|result_feedback ...`,
and after the run completes send the one aggregated row with
`search_feedback.py send --run-dir <run>` (`needs_auth` is a normal quiet outcome).
