---
name: import-linkedin-network
description: Import a LinkedIn Connections.csv export into Powerpacks-local people.csv artifacts using shared enrich_people delegation and run/continue/approve.
---

# Import LinkedIn Network

Use this skill when the user wants to import LinkedIn connections.

Do not depend on an external legacy app checkout. All code and artifacts live in Powerpacks.
External API enrichment via the shared RapidAPI-only `enrich_people` primitive is spend-bearing only for cache misses and must be approved after the local CSV conversion step. Seed `--profile-cache-dir` to avoid paid calls when possible. A hidden row cap exists only for tiny local smoke tests; do not use caps in real workflows.

## Main loop

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/linkedin/network_import.py run \
  --csv <Connections.csv> \
  --source-user <user-label> \
  --operator-id <operator-id-or-local>
```

If blocked and the user approves spend:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/linkedin/network_import.py approve
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/linkedin/network_import.py continue
```

## Output summary

Report only:

- ledger path
- run directory
- parsed connection count
- whether enrichment is blocked/completed
- canonical `people.csv` path when produced
- cache hit / paid call counts from the delegated enrichment summary

Do not paste CSV contents or raw provider JSON into chat.
