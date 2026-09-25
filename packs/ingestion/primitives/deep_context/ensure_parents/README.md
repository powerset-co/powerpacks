# deep_context/ensure_parents — `deep_ensure_parents`

`deep_ensure_parents` (`EnsureParents`) get-or-creates stable parents for every
row in the current fan-in export. It is the one declared node in this package.

Pipeline-wide context: [deep-context-pipeline.md](../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../skills/deep-context/SKILL.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_ensure_parents` | `merged/people.csv` (external), `deep-context/deep-context.sqlite` (external) | — (writes SQLite parents/people) | none of its own (`manifest = ""`) |

Both inputs are `external`: the graph does not model the SQLite projection, and
`merged/people.csv` is the fan-in import's output.

## Manifest / status

No `manifest.json`; `EnsureParentsManifest` (`source`, `people_projected`) is
returned to the caller with status `completed`. The Node template's `not_ready`
/ `failed` apply to its declared inputs.

## Control

Free.

## Invariant

Parent identity is get-or-create-or-absorb: `parent_id` is opaque and immutable
once minted and is never re-derived from membership — clustering strategy does
not change ids.
