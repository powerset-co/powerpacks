# `$search` deep mode — canonical legacy recruiting

Until the atomic recruiting cutover, the canonical recruiting owner is
`packs/search/primitives/deep_search/deep_search_loop.py`. Its supporting
`search_network_pipeline`, task-state, ledger, wide-search, judge, consensus,
and expansion owners remain live. Do not replace this workflow with the typed
candidate path during a normal recruiting request.

## One Review before retrieval

Write the complete pasted JD or role brief to `<run>/jd.txt`, or pass a public
job-posting URL with `--jd-url`. The first invocation builds and critiques the
plan, writes the legacy review artifacts under `<run>/epoch0/`, returns
`awaiting_plan_approval`, and stops before sourcing:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt --run-dir <run> --created-at <iso> \
  --backend <local|powerset> [--db <duckdb>] [--set-id <set>]
```

For a URL, replace `--jd-file ...` with `--jd-url <url>`. Review the exact plan,
critic findings, recruiter-policy provenance, core groups, seniority/hireability
policy, and structured location scope. User edits outrank JD inference, which
outranks versioned defaults.

Resume only after the user approves that exact plan:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-file <run>/jd.txt --run-dir <run> --created-at <iso> \
  --backend <local|powerset> [--db <duckdb>] [--set-id <set>] \
  --plan-approved
```

`--plan-approved` is the legacy CLI equivalent of the typed candidate field
`recruiting.plan_approved=true`: it approves the reviewed recruiting plan only.
It does not approve a judge, spend, or a paid quality run.

## Judge and paid-run approval

There is no deterministic production plan or judge default. Plan extraction and
the advisory critic are model-backed unless an already reviewed plan is supplied
through the supported legacy input. The selected production judge is also
model-backed: `--judge gpt` is paid, while `--judge codex` uses the configured
subscription CLI and remains a model judge rather than a deterministic fallback.

Before any paid plan, critic, triage, or judge call, obtain explicit user approval
for that spend-bearing execution. For the typed additive candidate path,
`recruiting.plan_approved=true` and `recruiting.judge_approved=true` are required
to select approved adapters, but those booleans and the presence of credentials
do **not** authorize a paid quality run. Quality validation requires a separate,
immediately preceding approval naming the cases, model, candidate/call caps,
estimated maximum spend, and private output directory.

If paid execution has not been approved, stop with `needs_input` before the paid
call. Do not silently substitute a deterministic plan/judge or another provider.

## Execution contract

After approved resume, the legacy loop owns diverse bounded probes, partial/all
probe failure semantics, hydration, conservative triage, the selected evidence
judge, deterministic gates, anchor expansion, convergence, and shortlist
persistence. Preserve its full provenance and distinct failure/stop reasons.

Canonical legacy outputs remain under the run directory, including the approved
plan/binding, task/ledger state, sourced union/frontier, judge artifacts,
consensus, and `shortlist/{shortlist_ranked,sendable_ranked,bench_ranked}.json`.
Do not delete compatibility artifacts while live readers remain.

## Typed candidate validation seam

`packs.search.pipeline.search.run_search(SearchSpec)` is additive and opt-in
before cutover. Use it only for deterministic tests or explicitly approved
read-only real-environment comparison against the canonical legacy run. It must
not become the user-facing recruiting owner, mutate production state, make an
unapproved paid call, or justify deletion of legacy owners.
