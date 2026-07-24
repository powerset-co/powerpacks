---
name: import-twitter
description: Import or smoke-test Twitter/X network artifacts through the RapidAPI + MOE + LinkedIn resolution pipeline. Use for $import-twitter.
---

# import-twitter

Use this skill for `$import-twitter` or Twitter/X network import testing.

This is an alias/wrapper around `discover/twitter/network_import.py`. It is
manifest-only: one idempotent `run` per handle writes the fixed dir
`.powerpacks/network-import/discover/twitter/<handle>/` (overwritten in place)
plus a single `manifest.json`. The RapidAPI-backed crawl and the OpenAI/RapidAPI
resolution steps are spend-bearing and run only with `--approve-spend`.

## 10-row sanity command

Without `--approve-spend`, `run` stops at the first spend step and emits a
`needs_approval` manifest naming the step + estimated calls:

```bash
uv run --project . python packs/ingestion/primitives/discover/twitter/network_import.py run \
  --handle example_operator \
  --max-pages 1 \
  --limit 10 \
  --min-score 0
```

Approve the spend by re-running the same command with `--approve-spend`; it
advances the whole pipeline in one pass and never re-spends steps whose output
is already on disk:

```bash
uv run --project . python packs/ingestion/primitives/discover/twitter/network_import.py run \
  --handle example_operator \
  --max-pages 1 \
  --limit 10 \
  --min-score 0 \
  --approve-spend \
  --linkedin-workers 10 \
  --aggregator-workers 10
```

Inspect progress any time from the manifest (no spend):

```bash
uv run --project . python packs/ingestion/primitives/discover/twitter/network_import.py status --handle example_operator
```

## Notes

- Requires a RapidAPI Twitter key subscribed to `twitter241` (`RAPIDAPI_TWITTER_KEY` or `RAPIDAPI_KEY`).
- MOE uses OpenAI and is spend-gated behind `--approve-spend`; do not run deep research for this smoke.
- RapidAPI LinkedIn validation uses `RAPIDAPI_LINKEDIN_KEY` or `RAPIDAPI_KEY` and is spend-gated behind `--approve-spend`.
- Resume is by artifact: a rerun skips steps whose fixed output CSV is already present, so it never re-crawls or re-spends. Delete the handle dir to force a clean re-run.
- Final output should include `people.csv`; summarize as `x/10 linkedins` plus counts, not raw rows.

After Twitter finishes, rebuild the merged network and local index with the
indexing fan-in:
`uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run --operator-id <operator-id>`.
