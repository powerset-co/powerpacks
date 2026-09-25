# discover/gmail — `gmail_stage_merge`, `gmail_account_extract`

Two declared nodes share this package: `gmail_stage_merge` (`GmailDiscovery`,
`discover.py`) is the store/orchestrator for one Gmail discovery run;
`gmail_account_extract` (`GmailAccountChannel`, `discover.py`) is one selected
account, spawned once per `--account-email`.

Declared in `pipeline/graph.py`; contract in `pipeline/contract.py`. Stage
overview: [discover/README.md](../README.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `gmail_stage_merge` | `discover/gmail/{account_slug}/linkedin_resolution_queue.csv` (optional) | `discover/gmail/linkedin_resolution_queue.csv` (full_rewrite) | `discover/gmail/manifest.json` (`GmailDiscoveryCompleted`) |
| `gmail_account_extract` | `~/.msgvault/msgvault.db` (external, optional) | `discover/gmail/{account_slug}/linkedin_resolution_queue.csv`, `discover/gmail/{account_slug}/people.csv` (full_rewrite, optional) | none (`manifest = ""`); the store publishes its `record` in the stage manifest's `children` |

Paths are under `.powerpacks/network-import/`.

## Manifest / status

`GmailDiscoveryCompleted` status `completed`; `GmailDiscoverySkipped` status
`skipped` (empty account list); `GmailDiscoveryFailed` status `failed`. The
per-account `GmailAccountExtracted` carries `status: completed` and the account's
`record` (including the engine's own status, which is not the node's status).
The account node's `GmailDiscoveryFailed` short-circuits the store's run loop.

## Control

Free — metadata-only, no enrichment providers.

## Invariant

Account selection is `--account-email` only; every run is a full rewrite
(`extract_gmail` re-derives whole-store totals), and the store stops at the first
failed channel.
