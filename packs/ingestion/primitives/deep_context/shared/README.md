# deep_context/shared — `deep_owner`

`deep_owner` (`BuildOwner`, `build_owner.py`) writes `owner.json` (your bio) and
projects the owner profile into the canonical SQLite store. It is the one
declared node in this package.

Pipeline-wide context: [deep-context-pipeline.md](../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../skills/deep-context/SKILL.md). Declared in
`pipeline/graph.py`; contract in `pipeline/contract.py`.

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_owner` | `profile_cache_v2/{public_identifier}.json` (external, optional) | `.powerpacks/deep-context/owner.json` (full_rewrite) | none of its own (`manifest = ""`); the payload is emitted by the caller |

`(external)` = no node in the graph produces the path.

## Manifest / status

No `manifest.json`. `BuildOwnerManifest` is returned (and emitted by the CLI).
Status values: `written` (profile written + projected), `exists` (an existing
`owner.json` reused and re-projected), `error` (unreadable/invalid JSON, no
`--linkedin-url` and no `owner.json`, or the profile fetch failed), plus the
Node template's `not_ready` / `failed`.

## Control

Free to run. Cache-first profile hydration; a cache miss hydrates through the
Powerset gateway (`POWERSET_API_KEY`). No in-code spend gate.

## Invariant

An existing `owner.json` is trusted as-is, however old — there is no freshness
check against LinkedIn, and only `--force` re-fetches. Downstream the owner is
required, not optional: `selection.build_system_prompt` raises without one.
