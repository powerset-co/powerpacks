# Testing

Use these checks before handing Powerpacks to users.

## Local Readiness

```bash
scripts/test-search-network check
```

This installs the current skills into `~/.codex/skills`, runs lint, runs unit
tests, and writes one extraction-harness prompt without invoking Codex or
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

## Headless Codex Extraction

Headless extraction answers: can Codex use the installed
`extract-search-query` skill to produce the right payload before primitives run?
The wrapper uses `codex exec`, writes the final model message to JSON, then
locally validates that it contains a `role_search_filters.semantic_query`.

Dry-run prompt generation:

```bash
scripts/test-search-network agent-extract-dry-run --bucket education --max-cases 1
```

Live Codex extraction plus primitive execution:

```bash
scripts/test-search-network agent-extract --bucket education --max-cases 1
```

Optional model override:

```bash
CODEX_MODEL=gpt-5.5 scripts/test-search-network agent-extract --case-glob stanford --max-cases 1
```

Environment knobs:

- `CODEX_MODEL`: optional model override for `codex exec`.
- `APP_DIR`: defaults to `/Users/arthur/workspace/aleph-mvp`.
- `RECALL_DIR`: defaults to `$APP_DIR/tests/recall`.
- `ENV_FILE`: retrieval primitive env file. Defaults to `.env` relative to
  `APP_DIR`; use an absolute path if you want to force a specific file.
- `LIMIT_CAP`: defaults to `1000`.

The live harness stores per-case prompts, extracted JSON, raw Codex logs, task
state, and primitive logs under:

```text
/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/agent-recall-parity/
/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/agent-recall-parity-logs/
```

## What To Inspect

- `*.prompt.txt`: exact prompt sent to headless Codex.
- `*.extracted.json`: query decomposition produced by Codex.
- `*.raw.log`: Codex command/stdout/stderr.
- task state JSON: planned steps versus actual `steps[]`.
- `packs/search/evals/recall_parity.md`: pass/fail rollup.
- `packs/search/evals/company_search.md`: company lookup pass/fail rollup.

## Test Gate

For a small external test, require:

- `scripts/test-search-network check` passes.
- Representative primitive recall buckets pass or have documented known gaps.
- `scripts/test-search-network company-dry-run` passes.
- At least 5 headless extraction cases produce schema-valid JSON.
- For real searches, every run returns a task state path plus CSV/JSONL/manifest
  artifacts.
