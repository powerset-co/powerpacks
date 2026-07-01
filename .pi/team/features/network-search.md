# Feature Owner: Powerpacks — Network Search

## Mission
Own reusable network-search, people-search, and company-search skills/primitives/evals.

## Primary scope

```txt
skills/search/
skills/search-company/
primitives/execute_role_search/
primitives/execute_search_slice/
primitives/resolve_companies/
docs/workflows/
evals/
tests/test_company_search_harness.py
```

## Responsibilities

- `search` skill behavior and docs
- company/people search primitives
- query decomposition and slice/role workflows
- recall/harness parity evals
- contracts consumed by Network Search app/API agents

## Invariants

- Keep powerpack primitives reusable and app-repo agnostic.
- Preserve schema contracts under `schemas/` and `contracts/`.
- Prefer deterministic fixture/eval runs before live data access.
- Ask before changing public primitive contracts.

## Regression checks

```bash
bash scripts/test-search
uv run pytest tests/test_company_search_harness.py tests/test_core_layout.py
uv run python evals/run_company_search_harness.py
```

## Startup checklist

1. Read this dossier and `.pi/team/manifest.yaml`.
2. Read `skills/search/SKILL.md`, `skills/search-company/SKILL.md`, and relevant workflow docs.
3. Summarize the search primitive/skill contract before editing.
