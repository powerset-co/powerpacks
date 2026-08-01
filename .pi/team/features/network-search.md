# Feature Owner: Powerpacks — Search

## Mission

Own the reusable typed lookup, GTM, and recruiting engine plus its public
`$search` router, backend contracts, documentation, and evals.

## Primary scope

```txt
packs/search/skills/search/
packs/search/skills/search-sql/
packs/search/pipeline/
packs/search/backends/
packs/search/contracts/
packs/search/docs/
packs/search/evals/
```

## Responsibilities

- one `$search` router for lookup, GTM, and recruiting
- typed `SearchSpec`, person-grain frontier, and `StageResult` contracts
- company-constrained people search through GTM
- local company-only relational/directory routing through `$search-sql`
- deterministic fixture/eval coverage and backend contract parity

## Invariants

- There is no standalone public company-search command.
- Select one concrete backend in the composition root; never fall back.
- Preserve schema contracts under `schemas/` and `contracts/`.
- Prefer deterministic fixture/eval runs before live data access.
- Ask before changing public primitive contracts.

## Regression checks

```bash
scripts/test-search-network component
uv run --project . python -m unittest tests.test_layered_search_engine tests.test_search_decision -v
```

## Startup checklist

1. Read this dossier and `.pi/team/manifest.yaml`.
2. Read `packs/search/skills/search/SKILL.md` and the relevant typed pipeline docs.
3. Summarize the public route and typed engine contract before editing.
