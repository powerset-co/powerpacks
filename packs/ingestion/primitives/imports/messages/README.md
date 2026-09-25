# imports/messages — `messages_import`

`messages_import` (`MessagesImport`, `importer.py`) imports message-discovery
contacts as candidate `people.csv` rows. It is the one declared node in this
package.

Declared in `packs/ingestion/primitives/pipeline/graph.py`; contract in
`packs/ingestion/primitives/pipeline/contract.py`. Stage overview:
[imports/README.md](../README.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `messages_import` | `messages/contacts.csv` (optional) | `import/messages/people.csv` (full_rewrite) | none of its own (`manifest = ""`); the imports writer writes `import/messages/manifest.json` |

Paths are under `.powerpacks/network-import/`; `messages/contacts.csv` is the
discovery merge's output.

## Manifest / status

`MessagesImportManifest` status `completed`, or `skipped` when a non-ready input
is reported by the source import; unchanged inputs return the existing manifest
(`noop`). The Node template's `not_ready` / `failed` apply.

## Control

Free.

## Invariant

It retains every source contact that clears the floor (`util.contact_floor_reason`:
usable identifier, researchable name, at least one message) without resolving
identity — no identity or worth decision, and no `directory.csv` read or write. The manifest belongs to
the imports writer (`imports/common.write_manifest`), so the declared
`manifest = ""` keeps the Node template from overwriting it.
