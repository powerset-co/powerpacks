# deep_context/merge_candidates — `deep_parents`, `deep_cluster`

Two declared nodes share this package (one directory, two modules and two
manifests): `deep_cluster` (`cluster_merge_candidates.py`) finds same-person
merge candidates with free name and identifier rules plus positive JEV identity and names checks;
`deep_parents` (`build_parents.py`) applies the accepted merges to parent
families and rewrites only the changed parent dossiers. There is no
`--approve` between them: cluster writes proposals, parents applies them.

Pipeline-wide context: [deep-context-pipeline.md](../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../skills/deep-context/SKILL.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_cluster` | — (reads SQLite facts/verdicts) | `deep-context/merge-candidates.csv` (full_rewrite), `deep-context/merge-candidates.md` (full_rewrite), `deep-context/jev/{request_sha256}.json` (optional) | `deep-context/dossiers/merge_manifest.json` (`ClusterMergeManifest`) |
| `deep_parents` | — (reads SQLite) | `deep-context/parents/{slug}.md` (upsert, optional) | `deep-context/parents/manifest.json` (`BuildParentsManifest`) |

## Manifest / status

`ClusterMergeManifest` status `completed` (its free `--dry-run` path emits a
`status: "dry_run"` estimate instead, without a manifest).
`BuildParentsManifest` status `completed`; the Node template adds `not_ready`
(required input unreadable) and `failed`.

## How a pair is decided

| step | what it takes | decided by | cost |
|---|---|---|---|
| 1 | a parent reachable only at role addresses (`ir@`, `billing@`): a shared mailbox | left out of the survey | free |
| 2 | source contact email, phone, email handle, or name buckets propose a pair | only compatible names remain; extracted identifier claims cannot propose a pair | free |
| 3 | identical normalized name and a shared source contact phone or email | merged unless a stored different-person verdict blocks it | free |
| 4 | all other pairs, including the same name without a source identifier tie | positive JEV pair judgment at p(yes) ≥ 0.5, then JEV names compatibility ≥ 0.5 | about $0.0001 a pair |

The pair judge requires affirmative evidence connecting the same individual.
Shared names, an office number, compatible lives, or missing contradictions do
not establish identity. Extracted identifier claims remain context and require
ownership evidence. The names check establishes compatibility only.

A shared first name, last name or email handle alone does not pair two
incompatible names. One-word names meet full names only through source contact
identifiers or source email handles. A stored different-person verdict blocks
transitive acceptance that would join its two children.

## Control

- `deep_cluster`: paid, cents. It makes one JEV request per uncached pair and
  a names request for each positive LLM decision. Answers cache under
  `deep-context/jev/` and verdicts in SQLite. The free `--dry-run` prices both
  requests, with names requests as an upper bound. Acceptance between two
  parents is rewritten by every survey: an omitted pair is no longer accepted.
- `deep_parents`: free. It applies accepted components; it does not split
  children already joined under one parent.

## Invariant

`deep_parents` merges each absorbed family in one `db.merge_parents` transaction
and writes only changed dossiers; parent ids are opaque and immutable, so
clustering never re-derives an id. `deep_cluster` keeps paid verdicts outside the
current blocking survey (`replace_merge_verdicts`) so a re-survey cannot erase
them.

## Changelog

- 2026-10-02: source identifiers and compatible names select pairs; same-name
  pairs need positive identity evidence unless a source identifier proves the
  exact-name duplicate.

- 2026-10-01: the same name merges without the pair judge once JEV finds
  nothing in the facts that keeps the records apart; a shared first name, last
  name or email handle alone is no longer a pair; shared mailboxes are left
  out of the survey.
- 2026-10-01: JEV answers whether a judged pair's two names can be one
  contact's; the spelling check and its hand-written nickname list are gone.
- 2026-09-25: JEV replaces the OpenAI pair judge; merge cutoff is p(yes) ≥ 0.5.
