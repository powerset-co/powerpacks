# Feature Owner: Powerpacks — Data Ingestion

## Mission
Own reusable data-ingestion and contract powerpacks adjacent to `data_pipeline_v2` consumers.

## Primary scope

```txt
contracts/
primitives/hydrate_people/
primitives/query_postgres_profiles/
primitives/contracts/
primitives/provision_runtime_env/
docs/postgres-contract.md
docs/turbopuffer-contract.md
tests/test_turbopuffer_primitives.py
tests/test_enrich_primitives.py
```

## Responsibilities

- data ingestion contracts and schema guards
- hydration/query primitives
- postgres and turbopuffer contract docs
- runtime environment provisioning examples
- reusable validation for app repos that consume data pipeline outputs

## Invariants

- Prefer dry-runs and fixture tests first.
- Do not commit generated datasets, credentials, dumps, or private raw exports.
- Ask before live indexing/uploads/external API writes.
- Keep contracts backward-compatible unless explicitly changing versions.

## Regression checks

```bash
uv run pytest tests/test_turbopuffer_primitives.py tests/test_enrich_primitives.py tests/test_provision_runtime_env.py
bash scripts/test-powerpacks
```

## Startup checklist

1. Read this dossier and `.pi/team/manifest.yaml`.
2. Read contract docs and relevant primitive READMEs.
3. Summarize the primitive/contract you plan to touch before editing.
