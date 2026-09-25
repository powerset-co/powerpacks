# imports/linkedin — `linkedin_import`

`linkedin_import` (`LinkedInImport`, `network_import.py`) is the idempotent
LinkedIn `Connections.csv` import: convert, then delegate enrichment, then write
the one discover-dir manifest. It is the one declared node in this package.

Declared in `packs/ingestion/primitives/pipeline/graph.py`; contract in
`packs/ingestion/primitives/pipeline/contract.py`. Stage overview:
[imports/README.md](../README.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `linkedin_import` | `Connections.csv` (external, optional) | `discover/linkedin/people.csv` (upsert, optional) | `discover/linkedin/manifest.json` (`LinkedInImportManifest`) |

Paths are under `.powerpacks/network-import/`.

## Manifest / status

`LinkedInImportManifest` carries `counts`, `artifacts`, `steps`, and
`needs_approval`. Status values: `completed`, `needs_approval` (the delegated
enrichment run would bill RapidAPI fetches without approval), `failed`, plus the
template `not_ready` / `failed`.

## Control

Paid via the in-process `enrich_people.EnrichPeople` delegation; `--approve-spend`
authorizes RapidAPI fetches for cache misses. `--convert-only` completes without
enriching.

## Invariant

The enrichment delegate writes its own `manifest.json` first; this stage's
manifest is written last and is the authoritative one for the fixed dir, so the
enriched `people.csv` and provider artifacts land in the same dir. `Connections.csv`
is `external` (the user's download) and `required=False` so a missing path still
produces this node's own `failed` manifest naming the file.
