# deep_context/merge_candidates — `deep_parents`, `deep_cluster`

Two declared nodes share this package (one directory, two modules and two
manifests): `deep_cluster` (`cluster_merge_candidates.py`) finds same-person
merge candidates with free name and identifier rules plus two paid JEV checks;
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

Each step takes what the one before left.

| step | what it takes | decided by | cost |
|---|---|---|---|
| 1 | a parent reachable only at role addresses (`ir@`, `billing@`): a shared mailbox | left out of the survey | free |
| 2 | the pair shares no phone or whole email, and neither name can be a form of the other | never paired | free |
| 3 | identical name and a shared contact phone or email | merged | free |
| 4 | the same name: the same words in any order, or the same first and last name where a middle name is missing on one side or agrees | merged unless JEV, reading both records' facts, puts the chance they must be kept apart at 0.4 or more | about $0.00003 a pair |
| 5 | everything else that was paired: a short or variant form of the name ("Jordan" / "Jordan Bravo", "J Bravo", "Jordan B", "Jon" / "John"), or a shared phone or email under two names | the JEV pair judge at p(yes) ≥ 0.5, then JEV on the two names alone | about $0.0001 a pair |

A shared first name, a shared last name or a shared email handle alone is not
a pair (step 2). A one-word name meets a full name only through a shared email
handle, phone or email. A generation suffix on one side (Jr, Sr, III) is not
the same name. A stored "two people" between two parents outranks steps 3
and 4.

The step 4 bar was set on 127 same-name pairs from two real installs, each
with a separate dossier on both sides, labeled blind and without LinkedIn. At
0.4: 54 merged, 52 of them labeled one person; 24 of 26 labeled two people
and all 10 that were not a person stayed apart; 25 of the 29 with a concrete
tie beyond the name merged. On the half held out from choosing the wording,
24 merged and all 24 were labeled one person.

## Control

- `deep_cluster`: paid, cents. The same-name check is one JEV request per
  same-name pair; the pair judge is one request per remaining pair, plus the
  two names alone for each pair it calls one person (about $0.00002). Answers
  cache under `deep-context/jev/` and verdicts in SQLite, so re-runs do not
  re-bill. Free `--dry-run` prices all three, the names requests as an upper
  bound. Acceptance between two parents is rewritten by every survey: a pair
  it does not return is no longer accepted.
- `deep_parents`: free.

## Invariant

`deep_parents` merges each absorbed family in one `db.merge_parents` transaction
and writes only changed dossiers; parent ids are opaque and immutable, so
clustering never re-derives an id. `deep_cluster` keeps paid verdicts outside the
current blocking survey (`replace_merge_verdicts`) so a re-survey cannot erase
them.

## Changelog

- 2026-10-01: the same name merges without the pair judge once JEV finds
  nothing in the facts that keeps the records apart; a shared first name, last
  name or email handle alone is no longer a pair; shared mailboxes are left
  out of the survey.
- 2026-10-01: JEV answers whether a judged pair's two names can be one
  contact's; the spelling check and its hand-written nickname list are gone.
- 2026-09-25: JEV replaces the OpenAI pair judge; merge cutoff is p(yes) ≥ 0.5.
