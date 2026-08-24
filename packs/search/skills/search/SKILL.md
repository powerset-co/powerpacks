---
name: search
description: "Route lookup, GTM, recruiting, SQL, and contacts searches, then execute engine searches through one persisted typed SearchSpec."
---

# Search

Use one visible flow:

1. Decide and persist the `SearchRoute` in `<run>/decision.json`.
2. For `target=engine`, write the complete typed `SearchSpec` to
   `<run>/search_spec.json`.
3. Review the persisted spec (and, for recruiting, complete the two-pass Review
   in `deep-mode.md`).
4. Execute the persisted spec through the canonical composition root.
5. Present status, counts, failures, approvals, and canonical artifacts.

Define `<run>` as `.powerpacks/search-runs/<run-id>`. Never retrieve from a
guessed route or an unpersisted engine request.

## Step 1 — route

<!-- decision-rules:start -->
For a request that is specific enough to route, persist exactly `target`,
`profile`, `backend`, and `reason`:

1. **target**
   - `engine`: person lookup, ordinary people search, or recruiting.
   - `sql`: local relational/aggregate questions and company-only local
     relational or directory questions.
   - `contacts`: contact-field or set-contact questions.
   - There is no public company-search target or replacement company command.
     People at a company are an engine GTM search with company constraints.
2. **profile** (`engine` only; otherwise `null`)
   - `lookup`: a bare person name, email, phone, handle, or profile URL.
   - `gtm`: people by role/function/level/company archetype, including people at
     a company.
   - `recruiting`: JD, job-posting URL, role brief, shortlist/source request, or
     an explicitly deep/judged hiring request.
3. **backend** (`engine` only; otherwise `null`)
   - `local`: explicit local/offline/imported-network wording.
   - `powerset`: explicit Powerset/set/team/shared-network wording.
   - Unstated: use local only when a local DB exists and remote credentials do
     not; otherwise use Powerset. Explicit wording always wins.
4. If the target, requested people, role/domain, intended surface, local company
   corpus, or backend is ambiguous, stop with `needs_input`, ask one concise
   clarifying question, and perform no retrieval. Do not default an ambiguous
   request to GTM and do not write a guessed `decision.json`.
<!-- decision-rules:end -->

`target=sql` loads `packs/search/skills/search-sql/SKILL.md` and does not create
a `SearchSpec`. `target=contacts` loads
`packs/contacts/skills/search-contacts/SKILL.md` and does not create a
`SearchSpec`. There is no backend fallback and no Sales Navigator fallback.

## Step 2 — persist the typed engine request

For every `target=engine` route, write one schema-valid `search.spec.v1` document
to `<run>/search_spec.json`. Preserve the selected route's `profile` and
`backend`, explicit corpus, lookup input where applicable, role and person/company
filters, technology skills, soft criteria, SQL candidates (local only), bounds,
and recruiting input (recruiting only). An evaluation recruiting run may also
persist its frozen, independently reviewed pool as
`recruiting.review_pool_person_ids`; ordinary recruiting omits it. Do not add
unknown fields or silently convert an unsupported hard filter into prompt text.

Lookup is deterministic. Local lookup fields are capability-derived. Powerset
lookup is set-scoped and supports only `person_id`, `name`, `handle`, and
`profile_url`; email and phone return `unsupported_capability`.

For GTM, structured-only requests use `rank_mode="deterministic"` and make no
model rank call. Soft criteria require the exact typed semantic contract:

- `rank_mode="semantic"`
- `rank_model="gpt-5.6-luna"`
- `rank_reasoning_effort="medium"`
- `rank_approved=true` only after explicit approval immediately before the
  provider call

Semantic GTM remains one bounded rank pass and never enters recruiting triage,
judge, or expansion. Recruiting does not use the GTM Luna rank layer; follow
`deep-mode.md`.

## Step 3 — execute the canonical path

After required Review and approvals, invoke only:

```bash
uv run --project . python -m packs.search.pipeline.search \
  --spec <run>/search_spec.json \
  --output-dir .powerpacks/search-runs/<run-id>
```

Never dispatch ordinary GTM or recruiting through a prepare command, task
manifest, legacy loop, or alternate backend. A failed or unsupported selected
backend is visible and final; do not fall back to the other backend.

## Approval and paid-call boundary

`rank_approved`, `recruiting.plan_approved`, and
`recruiting.judge_approved` authorize only their named execution adapter after
the user approves that call. Credential presence and approval booleans do not
authorize a paid quality-validation run.

Before any spend-bearing plan, critic, semantic-rank, triage, or judge call,
obtain explicit approval immediately before execution. A paid quality run also
requires separate explicit approval naming the cases, model, candidate/call
caps, output path, and estimated maximum spend. Without the applicable approval,
return `needs_input` before the provider call. Do not silently substitute a
deterministic implementation, another provider, or another backend.

## Present

Report the `SearchRoute`, profile/backend when applicable, terminal status,
retrieval/hydration counts, visible failures or blocked approvals, and paths to
canonical `search_spec.json`, `result.json`, `candidates.jsonl`,
`candidates.csv`, `hard-filter-validation.json`, and `manifest.json`. Recruiting
also reports its Review and shortlist artifacts from `deep-mode.md`.
