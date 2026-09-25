# deep_context/collection — `deep_collect`

`deep_collect` (`CollectPersonContext`) writes and projects one bounded message
bundle per SQLite parent. It is the one declared node in this package.

Pipeline-wide context: [deep-context-pipeline.md](../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../skills/deep-context/SKILL.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_collect` | `deep-context/deep-context.sqlite` (external, optional), `~/.msgvault/msgvault.db` (external, optional), `~/Library/Messages/chat.db` (external, optional), `messages/wacli/wacli.db` (external, optional) | `deep-context/raw/{parent_id}.json` (optional) | `deep-context/raw/manifest.json` (`CollectPersonContextManifest`) |

## Manifest / status

`CollectPersonContextManifest` status `completed`. It also records
`msgvault_available`, `chat_db_available`, `wacli_available`, per-channel
message counts, cap counts, and the `privacy` block. The Node template adds
`not_ready` / `failed`.

## Control

Free. Reads local message stores read-only; makes no provider calls.

## Invariant

Message stores are declared `external` and `required=False` on purpose: they may
legitimately be absent, and readiness is reported as counts (`*_available`, the
chat.db probe) rather than by refusing to run. Bundles under `raw/` are
ephemeral and gitignored; true totals are recorded so capping is honest.
