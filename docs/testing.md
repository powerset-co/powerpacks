# Testing

Use deterministic, offline checks before handing Powerpacks search changes to
users. Credential presence does not authorize network or model-backed tests.

## Typed engine and routing

The canonical search path consumes one schema-valid `SearchSpec`, selects one
concrete runner, and returns `StageResult` data over a person-grain
`CandidateFrontier`.

```bash
uv run --project . python -m unittest \
  tests.test_layered_search_engine \
  tests.test_search_decision \
  tests.test_search_backend_boundaries -v
```

These tests cover lookup/GTM/recruiting profile dispatch, early exits,
hard-filter ordering, frontier provenance, backend isolation, and routing:

- lookup, GTM, and recruiting route through `$search`;
- people-at-company requests are GTM with company constraints;
- company-only local relational/directory questions route to `$search-sql`;
- ambiguous requests stop before retrieval;
- there is no backend fallback or public company-search command.

## Recruiting

Recruiting uses the same persisted `SearchSpec` and composition root as lookup
and GTM. Its deterministic tests validate Review-before-retrieval, immutable
plan/source/corpus binding, bounded probes, partial/all-probe failure behavior,
hard-filter revalidation, triage/judge bounds, gates, expansion, and terminal
statuses.

```bash
uv run --project . python -m unittest tests.test_recruiting_pipeline -v
```

Do not run production plan, critic, triage, rank, or judge adapters without the
explicit approval required by the `$search` skill.

## Local SQL

`$search-sql` is local and read-only. It handles relational/aggregate questions,
including company-only local directory questions, after inspecting the selected
DuckDB schema.

```bash
uv run --project . python -m unittest tests.test_local_duckdb_query -v
```

## Offline quality validation

The layered-search validation runner reads committed cases and local artifacts;
it makes no network or model calls.

```bash
uv run --project . python -m unittest tests.test_layered_search_validation -v
uv run --project . python packs/search/evals/run_layered_search_validation.py \
  validate-cases --cases packs/search/evals/layered-search/cases.json
```

Private review pools, labels, corpus snapshots, and reports belong under
`.powerpacks/search-validation/`. Never commit candidate identities or contact
PII.

## Adapter and static checks

```bash
bash -n adapters/claude-code/install.sh \
  adapters/codex/install.sh \
  adapters/pi/install.sh \
  adapters/nanoclaw/install.sh

scripts/build-skills-map
```

Installers must install only current skills and scrub retired skill directories.
The generated skills map must reflect the tracked `packs/*/skills/*/SKILL.md`
surface.

## Live and paid lanes

Live retrieval requires an explicitly approved corpus/set scope and aggregate,
privacy-safe diagnostics. Paid quality validation additionally requires explicit
approval immediately before execution naming cases, model, candidate/call caps,
private output path, and maximum spend. Neither lane is part of routine local or
CI validation.
