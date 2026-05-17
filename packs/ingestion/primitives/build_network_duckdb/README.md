# build_network_duckdb

Load local network/contact artifacts into DuckDB for the local My Contacts / Network surface.

Inputs are produced by `merge_network_sources`:

- `people.csv`
- `network_contacts.csv`
- `network_contact_sources.csv`
- `network_companies.csv`

Run:

```bash
uv run --project . python packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py \
  --network-dir .powerpacks/network-import/merged \
  --output-dir .powerpacks/network-import/duckdb \
  --flavor local \
  --force
```

Outputs:

- `network.<flavor>.duckdb`
- `manifest.<flavor>.json`

DuckDB tables:

- `local_network_people`
- `local_network_contacts`
- `local_network_contact_sources`
- `local_network_companies`

Views:

- `network_people`
- `network_contacts`
- `network_contact_sources`
- `network_companies`

This primitive is local-only and does not call APIs or upload data.
