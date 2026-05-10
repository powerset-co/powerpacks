# merge_network_sources

Local cross-source merge/dedupe for network imports.

- Discovers `people_harmonic_all.csv` under `.powerpacks/network-import/*/*/`.
- Also maps `.powerpacks/messages/contacts.csv` into the shared people schema.
- Dedupe rule: merge exact LinkedIn public identifiers first.
- Similar names without shared LinkedIn are **not merged**; they are flagged in `possible_duplicates_review.csv`.

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

Outputs:

- `.powerpacks/network-import/merged/people_harmonic_all.merged.csv`
- `.powerpacks/network-import/merged/possible_duplicates_review.csv`
- `.powerpacks/network-import/merged/merge_manifest.json`
