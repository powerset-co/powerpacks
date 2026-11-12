# upload_powerset

Created: 2026-09-24

Change log:
- 2026-11-12: the share upload (CLI, plan, Postgres writes, manifest) was removed with
  the People page Share button. What is left is kept for network-search-api's index
  refresh (`scripts/powerset_v2/prepare_refreshed_indexes.py` imports `local_index`).
- 2026-10-08: the upload wrote the isolated share_v1 family, not v3.
- 2026-09-24: first version.

| file | role |
| --- | --- |
| `local_index.py` | local DuckDB readers for profiles and namespace documents |
| `models.py` | frozen value types + the channel/identifier table (`CLOUD_SOURCE_CHANNEL`, also read by deep search attribution) |
| `turbopuffer_writer.py` | namespace definitions (`NAMESPACES`, `NAMESPACE_BY_LOGICAL`) that `local_index` reads, and the write schema |

Nothing in the app writes Powerset Postgres or TurboPuffer from here any more.
