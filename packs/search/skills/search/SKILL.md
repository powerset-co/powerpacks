---
name: search
description: "The live search router. Bare-person lookup uses the typed deterministic path. Ordinary people searches use legacy search_network_pipeline; recruiting uses legacy deep_search_loop. Company, SQL, and contacts remain distinct live surfaces."
---

# Search

Create a visible five-step checklist:

1. Decide + record the search decision (`decision.json`)
2. Prepare the search (legacy payload preview or recruiting plan)
3. Review — confirm requirements with the user
4. Execute the search
5. Present results

Do not retrieve before Review.

## Step 1 — route

<!-- decision-rules:start -->
For a request that is specific enough to route, persist exactly `target`,
`profile`, `backend`, and `reason`:

1. **target**
   - `engine`: person lookup, ordinary people search, or recruiting.
   - `sql`: local relational/aggregate questions.
   - `contacts`: contact-field or set-contact questions.
   - Company-only lookup, IDs, investor/funding/sector, or company-set resolution
     remains the live `$search-company` surface. Dispatch there rather than
     coercing it into the typed `SearchRoute` contract.
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
4. If the target, requested people, role/domain, or intended surface is
   ambiguous, stop with `needs_input`, ask one concise clarifying question, and
   perform no retrieval. Do not default an ambiguous request to GTM and do not
   write a guessed `decision.json`.
<!-- decision-rules:end -->

`$search-network` remains a recognized legacy alias, including NanoClaw's live
`/search-network` command and its task-state/result UI.

## Live dispatch boundary

| request | canonical action before cutover |
|---|---|
| company-only lookup/resolution | load `packs/search/skills/search-company/SKILL.md` |
| relational/aggregate local query | load `packs/search/skills/search-sql/SKILL.md` |
| contact-field/set-contact query | load `packs/contacts/skills/search-contacts/SKILL.md` |
| bare-person lookup | execute `packs.search.pipeline.search` with `profile=lookup`; local fields are capability-derived, and Powerset supports `person_id`, `name`, `handle`, and `profile_url` only |
| recruiting | load `packs/search/skills/search/deep-mode.md`; use `deep_search_loop.py` |
| ordinary people search | use `search_network_pipeline.py prepare`, show its preview, then run its emitted approved command |

The legacy task manifest, task state, ledger, `search_network_pipeline`, and
deep-search orchestration remain live owners. Do not bypass, retire, or delete
them before the atomic cutover.

For an ordinary people search, prepare with the selected backend:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
  --backend <local|powerset> --query "<query>" --output-dir <run-dir>
```

For local, also pass `--db <duckdb>` when the configured default is not the
intended corpus. Present the emitted preview and ask exactly once:
**Execute this search or modify it?** Run only the emitted approved command.
Never fall back across backends or to Sales Navigator.

## Additive typed candidate path

`packs.search.pipeline.search` is the executable deterministic path for a
bare-person lookup because the legacy people-search pipeline has no equivalent
lookup operation. Local lookup fields come from the selected DuckDB columns.
Powerset lookup is set-scoped and supports only `person_id`, `name`, `handle`,
and `profile_url`; email and phone return `unsupported_capability`.

For GTM and recruiting, the typed composition root remains an additive,
explicit opt-in candidate path for deterministic tests and approved read-only
real-environment validation only. Using it must not change or suppress legacy
artifacts, task state, routing, or results. Do not use it for an ordinary GTM or
recruiting search merely because its module or schema exists.

The typed candidate path performs no paid quality run by implication. API
credentials, `plan_approved`, `judge_approved`, or any other boolean in a
`SearchSpec` are execution prerequisites only; none is authorization for paid
quality validation. Paid validation requires a separate explicit approval for
the named cases, model, bounds, output path, and estimated maximum spend.

## Present

Report the selected live surface/backend, status, retrieval/hydration counts,
visible failures or blocked approvals, and the legacy run/task/result artifact
paths. Do not describe typed candidate artifacts as canonical production output.
