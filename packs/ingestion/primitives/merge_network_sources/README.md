# merge_network_sources

Local cross-source merge/dedupe for network imports.

- Accepts only explicit `--input` paths; it never scans `.powerpacks` for run artifacts.
- Setup/import fan-in should pass stable per-source artifacts such as `gmail/people.gmail.csv` and `messages/people.messages.csv`.
- Explicit `messages/contacts.csv` inputs are mapped into the shared people schema.
- Dedupe rule: merge exact LinkedIn public identifiers first.
- Similar names without shared LinkedIn are **not merged**; they are flagged in `possible_duplicates_review.csv`.

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run \
  --input .powerpacks/network-import/gmail/people.gmail.csv \
  --input .powerpacks/network-import/messages/people.messages.csv
```

Outputs:

- `.powerpacks/network-import/merged/people.csv` — canonical merged people schema
- `.powerpacks/network-import/merged/people_harmonic_all.merged.csv` — temporary compatibility alias
- `.powerpacks/network-import/merged/possible_duplicates_review.csv`
- `.powerpacks/network-import/merged/merge_manifest.json`
