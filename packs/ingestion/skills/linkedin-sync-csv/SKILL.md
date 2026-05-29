---
name: linkedin-sync-csv
description: Import LinkedIn network data from LinkedIn's Connections.csv export into local Powerpacks artifacts.
---

# LinkedIn Sync CSV

Use `packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py`.

This is the stable LinkedIn ingestion path today.

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run \
  --csv <Connections.csv> \
  --source-user <label>
```

After a successful run, update account registry:

```bash
uv run --project . python packs/ingestion/primitives/account_registry/account_registry.py mark \
  --channel linkedin_csv --username <label> --artifact <people_csv> --success
```

External provider enrichment requires approval.
