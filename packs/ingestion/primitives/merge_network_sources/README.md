# merge_network_sources

Local cross-source merge/dedupe for network imports.

- Discovers provider-neutral people CSV exports under `.powerpacks/network-import/*/*/`.
- Also maps `.powerpacks/messages/contacts.csv` into the shared people schema.
- Dedupe rule: merge exact LinkedIn public identifiers first.
- Similar names without shared LinkedIn are **not merged**; rows may be marked with `needs_review=true` in the canonical output.

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

Outputs:

- `.powerpacks/network-import/merged/people.csv`
