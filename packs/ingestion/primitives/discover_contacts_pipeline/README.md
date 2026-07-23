# discover_contacts_pipeline

Changelog:
- 2026-07-23 (audit batch 16): deleted the legacy monolithic orchestrator
  (`discover_contacts_pipeline.py`) and the `$discover-contacts` skill; the
  LinkedIn discovery CLI (`linkedin/discover.py`) and its models went with
  them. Rewrote this README as the per-source package guide.
- 2026-07-23: Added the post-reorg layout section; fixed the stale claim that
  the CLI dropped `approve`/`continue` (it exposes run/continue/approve/status).

Per-source discovery primitives for local network ingestion. There is no
generic orchestrator: each import skill invokes its source's primitives
directly by file path.

## Layout

- `common.py` — shared helpers (CSV/JSON IO, accounts state, stage manifests,
  child-process runner).
- `directory.py` — `directory.csv` and `people.csv` materialization helpers.
- `discovery_config.py` + `discovery.config.json` — static discovery
  input/output contract for the gmail and messages sources.
- `gmail/` — msgvault sync (`sync.py`), discovery CLI (`discover.py`),
  metadata reader (`network_import.py`), LinkedIn resolution
  (`resolve_queue.py`), import step functions (`import_steps.py`).
- `linkedin/` — Connections.csv import + enrichment (`network_import.py`),
  run locally or inside the Modal sandbox via
  `packs/indexing/modal/run_linkedin.py`.
- `messages/` — iMessage/WhatsApp metadata discovery (`discover.py`,
  `extract_imessage.py`, `whatsapp_wacli.py`, `merge_contacts.py`,
  `normalize_contacts.py`).
- `twitter/` — Twitter/X import (`network_import.py`).

## Routing

| User command / skill | Source primitives used |
| --- | --- |
| `$import-gmail` | `gmail/discover.py discover` (sync + discovery), then `import_contacts_pipeline/gmail/importer.py` |
| `$setup` (LinkedIn) | Connections.csv placed at `.powerpacks/network-import/discover/linkedin/Connections.csv`, imported via `packs/indexing/modal/linkedin_modal_pipeline.py import-linkedin` (which runs `linkedin/network_import.py` in the sandbox) |
| `$import-messages` | `messages/discover.py discover`, then `import_contacts_pipeline/messages/importer.py` |
| `$import-twitter` | `twitter/network_import.py run/approve/continue` |

Each source writes stable artifacts under
`.powerpacks/network-import/discover/<source>/` (contacts/queue CSVs plus a
fingerprinted `manifest.json`) and overwrites in place; reruns are idempotent
because the output paths are fixed. Fan-in merge and the local search index are
owned by
`packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py`,
not by this package.

The shared `.powerpacks/network-import/directory.csv` is the reusable
email/phone/name → LinkedIn checkpoint; Gmail applies matching directory rows
first and writes filtered unresolved queues for later `$deep-context`
resolution.
