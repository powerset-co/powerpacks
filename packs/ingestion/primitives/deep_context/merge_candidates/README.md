# deep_context/merge_candidates — `deep_parents`, `deep_cluster`

Two declared nodes share this package (one directory, two modules and two
manifests): `deep_cluster` (`cluster_merge_candidates.py`) finds same-person
merge candidates with free name and identifier rules plus one Sol/high identity judgment;
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

## How a pair is decided

| step | what it takes | decided by | cost |
|---|---|---|---|
| 1 | a parent reachable only at role addresses (`ir@`, `billing@`): a shared mailbox | left out of the survey | free |
| 2 | source contact email, phone, email handle, or name buckets propose a pair | only compatible names remain; extracted identifier claims cannot propose a pair | free |
| 3 | identical normalized name and a shared source contact phone or email | merged unless a stored different-person verdict blocks it | free |
| 4 | all other pairs, including the same name without a source identifier tie | Sol/high: same, different, or uncertain | dry-run estimates input and output tokens |

The pair judge requires affirmative evidence connecting the same individual.
Shared names, an office number, compatible lives, or missing contradictions do
not establish identity. Extracted identifier claims remain context and require
ownership evidence. Uncertain is a completed judgment: it neither merges people
nor constrains a later proven connection. Different is affirmative evidence of
two people; it blocks both direct and transitive joins.

Every original source member name must be present and compatible with every
other member before a join. This applies to proposals, accepted receipts, and
parent application, including transitive joins. A representative cannot hide a
conflicting or unknown child name. Stored different-person decisions constrain
all joins; an internal child rejection prevents further merging of that parent.
Refreshing the survey does not erase a rejection. A fresh rebuild excludes old
machine decisions before the survey.

A shared first name, last name or email handle alone does not pair two
incompatible names. One-word names meet full names only through source contact
identifiers or source email handles. A stored different-person verdict blocks
transitive acceptance that would join its two children.

## Control

- `deep_cluster`: one `gpt-6.1-sol`/high request per uncached pair. The request
  signature covers system prompt, rendered source evidence, owner, schema,
  model, effort, and output limit. Each completed decision is saved to SQLite
  before another call finishes, with acceptance false until global constraints
  are checked. Unchanged uncertain decisions are reused; failed calls retry.
  `--dry-run` estimates input and 1,500 output tokens per request, not a ceiling.
  `--limit 1` runs one uncached pair. Acceptance is rewritten each survey;
  changed or omitted pairs cannot retain old acceptance.
- `deep_parents`: free. It applies accepted components; it does not split
  children already joined under one parent.

## Invariant

`deep_parents` merges each absorbed family in one `db.merge_parents` transaction
and writes only changed dossiers; parent ids are opaque and immutable, so
clustering never re-derives an id. `deep_cluster` keeps paid verdicts outside the
current blocking survey (`replace_merge_verdicts`) so a re-survey cannot erase
them.

## Changelog

- 2026-10-03: one structured Sol/high judgment replaces binary JEV and the names
  question; SQLite preserves uncertainty and checkpoints every completed call.

- 2026-10-03: all original source names and stored child rejections constrain
  proposals, receipts, and parent application.

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
