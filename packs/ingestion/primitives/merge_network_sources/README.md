# merge_network_sources

Local cross-source merge/dedupe for network imports.

- Discovers canonical `people.csv` under `.powerpacks/network-import/*/*/` first.
- Falls back to legacy `people_harmonic_all.csv` / `people_enriched.csv` when `people.csv` is absent.
- Also maps `.powerpacks/messages/contacts.csv` into the shared people schema.
- Dedupe rule: merge exact LinkedIn public identifiers first.
- Similar names without shared LinkedIn are **not merged**; they are flagged in `possible_duplicates_review.csv`.

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

Outputs:

- `.powerpacks/network-import/merged/people.csv` — canonical merged people schema
- `.powerpacks/network-import/merged/people_harmonic_all.merged.csv` — temporary compatibility alias
- `.powerpacks/network-import/merged/possible_duplicates_review.csv`
- `.powerpacks/network-import/merged/merge_manifest.json`
