# merge_network_sources

Local cross-source merge/dedupe for network imports.

- Accepts only explicit `--input` paths; it never scans `.powerpacks` for run artifacts.
- Product fan-in should pass reviewed, stable per-source artifacts such as
  `import/gmail/people.csv` and `import/messages/people.csv`.
- Raw `messages/contacts.csv` is not a canonical product fan-in input; it must
  pass through `$import-messages` review and materialization first.
- Dedupe rule: merge exact LinkedIn public identifiers first.
- Similar names without shared LinkedIn are **not merged**; they are flagged in `possible_duplicates_review.csv`.

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run \
  --input .powerpacks/network-import/import/gmail/people.csv \
  --input .powerpacks/network-import/import/messages/people.csv
```

Outputs:

- `.powerpacks/network-import/merged/people.csv` — canonical merged people schema
- `.powerpacks/network-import/merged/people_harmonic_all.merged.csv` — temporary compatibility alias
- `.powerpacks/network-import/merged/possible_duplicates_review.csv`
- `.powerpacks/network-import/merged/merge_manifest.json`
