# deep_context/merge_candidates — `deep_parents`, `deep_cluster`

Two declared nodes share this package (one directory, two modules and two
manifests): `deep_cluster` (`cluster_merge_candidates.py`) finds same-person
merge candidates with free identity gates plus a paid LLM judge;
`deep_parents` (`build_parents.py`) applies the accepted merges to parent
families and rewrites only the changed parent dossiers. There is no
`--approve` between them: cluster writes proposals, parents applies them.

Pipeline-wide context: [deep-context-pipeline.md](../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../skills/deep-context/SKILL.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_cluster` | — (reads SQLite facts/verdicts) | `deep-context/merge-candidates.csv` (full_rewrite), `deep-context/merge-candidates.md` (full_rewrite) | `deep-context/dossiers/merge_manifest.json` (`ClusterMergeManifest`) |
| `deep_parents` | — (reads SQLite) | `deep-context/parents/{slug}.md` (upsert, optional) | `deep-context/parents/manifest.json` (`BuildParentsManifest`) |

## Manifest / status

`ClusterMergeManifest` status `completed` (its free `--dry-run` path emits a
`status: "dry_run"` estimate instead, without a manifest).
`BuildParentsManifest` status `completed`; the Node template adds `not_ready`
(required input unreadable) and `failed`.

## Control

- `deep_cluster`: paid — bills the OpenAI pair judge for uncached pairs. Free
  `--dry-run` estimate; verdicts cache per judged pair so re-runs do not re-bill.
- `deep_parents`: free.

## Invariant

`deep_parents` merges each absorbed family in one `db.merge_parents` transaction
and writes only changed dossiers; parent ids are opaque and immutable, so
clustering never re-derives an id. `deep_cluster` keeps paid verdicts outside the
current blocking survey (`replace_merge_verdicts`) so a re-survey cannot erase
them.
