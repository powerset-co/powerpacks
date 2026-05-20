# Testing

Use these checks before handing Powerpacks to users.

## Local Readiness

```bash
scripts/test-search-network check
```

This installs the current skills into `~/.codex/skills`, runs lint, runs unit
tests, and dry-runs selected pipeline-eval cases without invoking expansion or
retrieval APIs. It also dry-runs the company-search harness.

## Company Search Harness

Company search answers: can the `search-company` skill decompose direct company
lookups, aliases, sectors, semantic verticals, funding/headcount constraints,
and investor-backed filters into resolver-ready payloads?

Dry-run contract validation:

```bash
scripts/test-search-network company-dry-run
scripts/test-search-network company-dry-run --case-glob investor
```

Live resolver execution:

```bash
scripts/test-search-network company-live --max-cases 2
```

The live mode creates task state, resolves investors when needed, runs
`resolve_companies`, and writes primitive logs under:

```text
/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/company-search/
/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/company-search-logs/
```

The rollup report is:

```text
packs/search/evals/company_search.md
```

## Primitive Recall

Primitive recall answers: if the query payload is correct, do the packaged
primitives retrieve representative data?

```bash
scripts/test-search-network primitive-recall --bucket education
scripts/test-search-network primitive-recall --bucket company
scripts/test-search-network primitive-recall --case-glob stanford --max-cases 2
```

This uses deterministic decomposition in `packs/search/evals/run_recall_parity.py`, then
runs resolver, prefilter, count, retrieval, hydration, and export primitives.
It writes the report to:

```text
packs/search/evals/recall_parity.md
```

## Parallel Query Expansion

Pipeline eval answers: can the parallel `expand_search_request` primitive
produce the right payload before primitives run? This is the same expansion path
used by `search_network_pipeline.py prepare`.

### CI-safe mocked prepare regression

Use this when you want to verify the harness-facing happy path without any live
credentials:

```bash
scripts/test-search-network mock-prepare
```

This starts an in-process OpenAI-compatible Chat Completions HTTP server, points
`OPENAI_API_BASE` at it, and runs the real command path:

```text
search_network_pipeline.py prepare -> expand_search_request -> 8 parallel extractor calls
```

It validates that `prepare` emits `preview_ready`, writes the payload artifact,
merges fields from multiple extractor responses, and returns an
`--execute-approved` command. It intentionally stops before retrieval, so it does
not require or validate TurboPuffer, Postgres hydration, or real LLM reranking.
Use live `pipeline-eval` for that full integration tier.

Dry-run selected recall cases:

```bash
scripts/test-search-network pipeline-eval-dry-run --bucket education --max-cases 1
```

Live expansion plus primitive execution:

```bash
scripts/test-search-network pipeline-eval --bucket education --max-cases 1
```

Optional model override:

```bash
EXPAND_SEARCH_MODEL=gpt-5.4-mini scripts/test-search-network pipeline-eval --case-glob stanford --max-cases 1
```

Environment knobs:

- `EXPAND_SEARCH_MODEL`: optional model override for parallel expansion.
- `APP_DIR`: defaults to `/Users/arthur/workspace/network-search-api` for
  pipeline eval.
- `RECALL_DIR`: defaults to `$APP_DIR/tests/recall`.
- `ENV_FILE`: retrieval primitive env file. Defaults to `.env` relative to
  `APP_DIR`; use an absolute path if you want to force a specific file.
- `LIMIT_CAP`: defaults to `1000`.

The live harness stores per-case extracted JSON, task state, and primitive logs
under:

```text
.powerpacks/pipeline-eval/extractions/
```

## What To Inspect

- `*.extracted.json`: query decomposition produced by `expand_search_request`.
- `*.expand.log`: primitive command/stdout/stderr.
- task state JSON: planned steps versus actual `steps[]`.
- `packs/search/evals/recall_parity.md`: pass/fail rollup.
- `packs/search/evals/company_search.md`: company lookup pass/fail rollup.

## Test Gate

For a small external test, require:

- `scripts/test-search-network check` passes.
- Representative primitive recall buckets pass or have documented known gaps.
- `scripts/test-search-network company-dry-run` passes.
- `scripts/test-search-network mock-prepare` passes in CI or locally.
- At least 5 live `pipeline-eval` cases produce schema-valid JSON when API
  credentials are available.
- For real searches, every run returns a task state path plus CSV/JSONL/manifest
  artifacts.
