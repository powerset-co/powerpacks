# `$search` deep mode — typed recruiting Review and resume

Recruiting uses the same persisted `SearchSpec` and the same canonical command
as lookup and GTM. It is a two-pass run in one unchanged output directory: the
first pass creates Review artifacts and stops before retrieval; the second pass
resumes only the exact reviewed plan.

Set `<run>` to `.powerpacks/search-runs/<run-id>` and keep the complete JD, role
brief, or public job-posting URL in `recruiting.source`. Persist the complete
`search.spec.v1` at `<run>/search_spec.json` with `profile="recruiting"`, the
selected backend/corpus, explicit bounds, and:

- `recruiting.reviewed_plan_hash=null` on the first pass;
- an explicit `recruiting.plan_model` and `recruiting.plan_approved=true` before
  production plan extraction and critic calls;
- an explicit `recruiting.judge_implementation` and
  `recruiting.judge_approved=true` before production judging. The judge model and
  reasoning effort default to `recruiting.judge_model="gpt-5.6-luna"` and
  `recruiting.judge_reasoning_effort="none"` — cheap and fast, so the filtering
  stage can be iterated on; set either field explicitly to override. The accepted
  efforts are `none`, `low`, `medium`, and `high`.
- for a Reflect evaluation only, the complete frozen independent pool in
  `recruiting.review_pool_person_ids`; omit this field for ordinary recruiting.

There is no deterministic production plan or judge fallback.

## Pass 1 — create Review artifacts

After approval for any spend-bearing plan and critic calls, invoke:

```bash
uv run --project . python -m packs.search.pipeline.search \
  --spec <run>/search_spec.json \
  --output-dir .powerpacks/search-runs/<run-id>
```

The expected status is `awaiting_review`. No source retrieval may occur. Review
all of these exact artifacts:

- `review/plan.json` — normalized recruiter plan, user/JD/default provenance,
  core groups, seniority/track and hireability policy, location scope, and
  must-have versus bonus evidence;
- `review/critic.json` — deterministic and advisory critic findings;
- `review/policy.json` — exact versioned recruiter policy snapshot;
- `review/source.json` — normalized source and JD/source hash;
- `review/corpus.json` — verified comparable corpus snapshot;
- `review/evidence.json` — exact evidence hashes for every requested review-pool
  person ID (empty for ordinary recruiting);
- `review/binding.json` — canonical plan, JD, source, corpus, review-pool, and
  policy binding.

Surface ambiguities instead of silently hardening them. User edits outrank JD
inference, which outranks versioned defaults. If the source is thin/invalid, the
corpus cannot be verified, or plan/critic execution is not approved, return
`needs_input` without retrieval.

## Pass 2 — bind and resume

After the user approves the exact Review, copy `plan_sha256` from
`review/binding.json` into `recruiting.reviewed_plan_hash` in the same
`<run>/search_spec.json`. Do not edit the reviewed plan or replace the source,
corpus, or policy snapshot. Then rerun the exact same command:

```bash
uv run --project . python -m packs.search.pipeline.search \
  --spec <run>/search_spec.json \
  --output-dir .powerpacks/search-runs/<run-id>
```

Resume loads the existing `review/plan.json` and `review/binding.json`, recomputes
the binding, and requires exact plan/source/JD/corpus/policy/review-pool equality. Missing
artifacts, a wrong hash, or drift returns `failed_binding`; start a new run
instead of repairing or bypassing the binding. Retrieval begins only after this
check and after an explicit approved judge is configured.

## Execution and paid-call boundaries

The resumed typed pipeline owns bounded differentiated probes, partial/all-probe
failure semantics, one person-grain frontier, hydration and hard-filter
revalidation, conditional conservative triage, one selected evidence judge,
deterministic gates, bounded anchor expansion, net-new-only judging, and distinct
converged/no-anchor/capped outcomes. Preserve all provenance and visible errors.

`recruiting.plan_approved=true` and `recruiting.judge_approved=true` approve only
their named adapters after explicit approval immediately before the spend-bearing
calls. They do not authorize a paid quality-validation run. Credentials do not
authorize one either. Paid quality validation separately requires explicit
approval naming cases, model, candidate/call caps, estimated maximum spend, and
private output directory. Without applicable approval, return `needs_input`; do
not substitute another model, provider, backend, plan, or judge.

Report the canonical top-level artifacts plus `review/*`, `shortlist_ranked.json`,
`sendable_ranked.json`, and `bench_ranked.json` when produced.
