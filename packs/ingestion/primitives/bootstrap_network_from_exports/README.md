# bootstrap_network_from_exports

Build reusable local network bootstrap bundles from existing operator export
CSVs. This is for importing prior enrichment/checkpoint work into Powerpacks
artifacts without hardcoding the source repository into runtime code.

## Generate

```bash
uv run --project . python packs/ingestion/primitives/bootstrap_network_from_exports/bootstrap_network_from_exports.py generate \
  --operator-mapping ../aleph-mvp/operator_mapping.json \
  --source-dir ../aleph-mvp/pipeline_output/unified/contact \
  --operators arthur,jake,jonathan,patrick \
  --linkedin-csv ~/Downloads/Complete_LinkedInDataExport_05-16-2026.zip/Connections.csv \
  --gmail-account-email thearthurchen@gmail.com \
  --seed-profile-cache \
  --force
```

Outputs are written under `.powerpacks/network-bootstrap/`:

- `operators/<operator>/manifest.json`
- `operators/<operator>/resolution/linkedin_resolutions.csv`
- `operators/<operator>/resolution/linkedin_resolutions_cached.csv`
- `operators/<operator>/enrichment/profile_cache_v2/`
- `operators/<operator>/outputs/commands.txt`
- `bundles/<operator>.tar.gz`

The primitive copies no secrets, raw Gmail DBs, or message bodies.
