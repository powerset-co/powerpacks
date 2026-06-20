---
name: setup
description: Run the full local Powerpacks setup/onboarding pipeline using canonical .powerpacks paths. Use for `$setup`, fresh-machine setup, or rebuilding local ingestion/index artifacts without inventing file paths.
---

# Setup

Use this skill for `$setup` and full local Powerpacks onboarding.

The file/path source of truth is:

- `powerpacks/docs/pipeline-file-dag.md`
- `powerpacks/packs/ingestion/pipeline_paths.py`

Do not invent alternate `.powerpacks` artifact paths. Do not pass `--input`,
`--output-dir`, `--ledger`, or `--run-id` for normal merge/enrich/index stages;
those flags are only for explicit one-off recovery/debug.

## Normal flow

1. Start or resume guided source setup:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py run
```

2. Follow the primitive JSON responses:

- `status: needs_agent_action` — run the returned `command` yourself, then run
  its `continue_command` or continue with `done`.
- `status: needs_user_action` — run any local command you can, then ask the user
  for only the remaining human action such as browser OAuth, QR scan, or export
  file path.
- `status: needs_user_input` — ask the question directly and continue with the
  user's reply.

Continue with:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --input <user-reply>
```

3. Merge all available local source artifacts into the canonical people CSV:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

4. Enrich from the canonical merge output when desired. Provider calls remain
approval-gated and cached/local rows run without spend:

```bash
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run
```

5. Build deterministic local search index records:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --force
```

## Read-only checks

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py status
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py plan
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status
```

## Canonical outputs

- `.powerpacks/ingestion/accounts.json`
- `.powerpacks/network-import/merged/people.csv`
- `.powerpacks/network-import/merged/review_pairs.csv`
- `.powerpacks/network-import/merged/manifest.json`
- `.powerpacks/network-import/enrichment/current/people_enriched.csv`
- `.powerpacks/search-index/current/records/*.records.jsonl`

If a path question comes up, read `docs/pipeline-file-dag.md` instead of
choosing a new location.
