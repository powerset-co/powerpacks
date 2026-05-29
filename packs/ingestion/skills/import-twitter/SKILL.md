---
name: import-twitter
description: Import or smoke-test Twitter/X network artifacts through the RapidAPI + MOE + LinkedIn resolution pipeline. Use for $import-twitter.
---

# import-twitter

Use this skill for `$import-twitter` or Twitter/X network import testing.

This is an alias/wrapper around `twitter_network_import`. The production source
crawl is RapidAPI-backed and requires approval. Keep artifacts under
`.powerpacks/network-import/twitter/`.

## Jake 10-row sanity command

```bash
uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run \
  --handle jake_zeller \
  --max-pages 1 \
  --limit 10 \
  --min-score 0 \
  --linkedin-workers 10 \
  --aggregator-workers 10
```

Then approve/continue as the primitive requests:

```bash
uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py approve
uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py continue
```

## Notes

- Requires a RapidAPI Twitter key subscribed to `twitter241` (`RAPIDAPI_TWITTER_KEY` or `RAPIDAPI_KEY`).
- MOE uses OpenAI and requires approval; do not run deep research for this smoke.
- RapidAPI LinkedIn validation uses `RAPIDAPI_LINKEDIN_KEY` or `RAPIDAPI_KEY` and requires approval.
- Final output should include `people.csv`; summarize as `x/10 linkedins` plus counts, not raw rows.

After Twitter finishes, run `$import-network --include-existing-artifacts` or the import-network primitive to merge into local network contacts and DuckDB.
