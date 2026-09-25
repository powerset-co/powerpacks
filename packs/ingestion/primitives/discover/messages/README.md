# discover/messages — `messages_stage_merge`

`messages_stage_merge` (`MessagesDiscovery`, `discover.py`) orchestrates one
messages discovery run: it creates the fixed output dir, runs each enabled
channel, merges the per-channel CSVs, and writes the stage manifest. It is the
one declared node in this package; the two per-channel extract nodes live in
[`channels/`](channels/README.md).

Declared in `pipeline/graph.py`; contract in `pipeline/contract.py`. Stage
overview: [discover/README.md](../README.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `messages_stage_merge` | `messages/imessage.contacts.csv` (optional), `messages/whatsapp.contacts.csv` (optional) | `messages/contacts.csv` (full_rewrite) | `discover/messages/manifest.json` (`MessagesDiscoveryCompleted`) |

Paths are under `.powerpacks/`; the stage manifest is under
`.powerpacks/network-import/`.

## Manifest / status

`MessagesDiscoveryCompleted` status `completed`; `MessagesDiscoverySkipped`
status `skipped`; `MessagesDiscoveryNotCompleted` status `failed`. Pairing
details surface as `whatsapp_pairing_state` / `whatsapp_pairing_notice`.

## Control

Free — metadata only, no provider calls.

## Invariant

The store holds all filesystem side effects so the channels stay pure, and it
stops at the first blocked/failed channel rather than merging a partial run.
Channel selection is the CLI's `--include-imessage` / `--include-whatsapp`
flags; there is no `accounts.json`.
