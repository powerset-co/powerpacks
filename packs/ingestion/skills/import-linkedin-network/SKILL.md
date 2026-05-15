---
name: import-linkedin-network
description: Import a LinkedIn Connections.csv export into Powerpacks-local provider enrichment artifacts and provider-neutral people.csv shape using run/continue/approve.
---

# Import LinkedIn Network

Use this skill when the user wants to import LinkedIn connections.

Do not depend on `../aleph-mvp`. All code and artifacts live in Powerpacks.
External API enrichment via Harmonic/RapidAPI is spend-bearing and must be
approved after the local CSV conversion step. A hidden row cap exists only for tiny local smoke tests; do not use caps in real workflows.

## Main loop

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run \
  --csv <Connections.csv> \
  --source-user <user-label> \
  --operator-id <operator-id-or-local>
```

If blocked and the user approves spend:

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py approve
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py continue
```

## Output summary

Report only:

- ledger path
- run directory
- parsed connection count
- whether enrichment is blocked/completed
- `people.csv` path when produced

Do not paste CSV contents or raw provider JSON into chat.
