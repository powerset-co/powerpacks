# account_registry

Manage local non-secret ingestion account state in:

`.powerpacks/ingestion/accounts.json`

The file is JSON so it can be read/written with stdlib only.
Do not store tokens or passwords here.

```bash
uv run --project . python packs/ingestion/primitives/account_registry/account_registry.py init
uv run --project . python packs/ingestion/primitives/account_registry/account_registry.py status
uv run --project . python packs/ingestion/primitives/account_registry/account_registry.py mark --channel twitter --username myhandle --success
uv run --project . python packs/ingestion/primitives/account_registry/account_registry.py mark --channel gmail --skipped --notes "operator skipped Gmail"
```
