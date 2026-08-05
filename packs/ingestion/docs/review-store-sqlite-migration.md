# Review store: CSV -> SQLite migration

Created: 2026-08-04

Changelog:
- 2026-08-05: P0 shipped (`review_db.py`). Ground-truth revisions vs the
  initial design, measured on both real stores (arthur 1292 rows, jake 16891):
  (1) `approved` stays a decision column — real stores carry approved=auto
  rows with human sources (jake: 95 x deep-context-review), so approved-class
  is NOT derivable from source-class; (2) there is no `people` table yet —
  uuid-keyed rows routinely carry identity cells (jake: 1244), so every
  non-parent row is a `links` row with a typed `kind`
  (pub|person_uuid|candidate_email|candidate_phone|message_linkedin — the
  sixth live namespace, message-linkedin:*, was missing from the draft);
  reference identities from index.json arrive in P1; (3) `worth_person_ids`
  lives on `parents` (machine membership bookkeeping), not on the worth
  decision; (4) the real `source` vocabulary is 11 writers (see
  `ReviewSource` in review_store.py), not the draft's invented 5. The value
  vocabulary is StrEnums in review_store.py; review_db.py generates its SQL
  CHECK constraints and INSERT statements from the same enums/dataclasses so
  the layers cannot drift. Gate result: byte-identical round-trip
  (review.csv AND synthetic-people.csv) on both stores.
- 2026-08-04: initial design (no code).

## 1. Problem

`overrides/review.csv` is one flat CSV keyed by `public_identifier`, but that key
carries **five namespaces**: plain LinkedIn pubs (`jordan-bravo-123`),
`candidate:email:<addr>`, `candidate:phone:<digits>`, `parent-worth:<parent_id>`,
and bare directory person-id UUIDs (a sixth decision store — the `approved` gate —
lives in `synthetic-people.csv`). The person card is a **runtime view**
(`review_web/model.py:build_parents` + `collapse_by_current_parent` over
`verdicts.jsonl` + `index.json`), so no durable row ever says "this person is
settled." Every decision door — `/decide`, `/worth`, the guided-retarget worker,
the judge-apply pass — must therefore fan one answer out into N per-row upserts
across two files, each write a **whole-file rewrite** (`write_override_rows`),
each door re-implementing its own sibling/gate/pruned-pub sweep and idempotence
guards. That shape produced this month's bug classes: **cycles** (half-decided
parents bouncing back into the queue; the retarget fail-closed fixes of
2026-08-04), **half-decided residue** (pre-v1.15.3 single-row `/decide` ->
legacy rule 4), **lock/mtime patches** (`review_rows_now` mtime cache, the
advisory session flock, the deleted 2026-07-29 stat/signature apparatus, the
retarget `blanked`-and-restore dance), and **boot scrubs** accreting in
`common/legacy.py`. 80+ commits this month fought consequences of the storage
shape rather than product behavior.

## 2. Schema

`review.sqlite` (stdlib `sqlite3`, `PRAGMA journal_mode=WAL`,
`foreign_keys=ON`, `busy_timeout=5000`), in `overrides/` next to `review.csv`.
Design rule: **policy is applied at write time** — the judge-apply pass writes
machine `decisions` rows (the confirm/detach/decisive bars stay in Python), so
reads and the queue view carry no thresholds; "pending" is literally "no
decision row."

```sql
CREATE TABLE people (
  person_id        TEXT PRIMARY KEY,          -- bare UUID | candidate:email:<addr> | candidate:phone:<digits>
  name             TEXT NOT NULL DEFAULT '',
  llm_worth        TEXT NOT NULL DEFAULT ''   -- machine worth mirrored from facts/<id>.jsonl
                     CHECK (llm_worth IN ('', 'yes', 'maybe', 'no')),
  llm_worth_reason TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE links (                          -- one row per (person, LinkedIn profile) pairing; machine judge state
  pub               TEXT PRIMARY KEY,         -- LinkedIn public identifier
  person_id         TEXT NOT NULL REFERENCES people(person_id),
  url               TEXT NOT NULL DEFAULT '',
  proposed_action   TEXT NOT NULL DEFAULT 'verify'
                      CHECK (proposed_action IN ('verify','detach','retarget','exclude')),
  new_url           TEXT NOT NULL DEFAULT '', -- retarget proposal target
  new_pub           TEXT NOT NULL DEFAULT '',
  confidence        REAL,
  reason            TEXT NOT NULL DEFAULT '',
  match_emails      TEXT NOT NULL DEFAULT '[]',  -- JSON arrays
  match_phones      TEXT NOT NULL DEFAULT '[]',
  judge_fingerprint TEXT NOT NULL DEFAULT '', -- paid-verdict cache key: NEVER regenerate on migration
  reject            TEXT NOT NULL DEFAULT '', -- llm_reject / _confidence / _reason
  reject_confidence REAL,
  reject_reason     TEXT NOT NULL DEFAULT '',
  updated_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX links_by_person ON links(person_id);

CREATE TABLE decisions (                      -- terminal outcomes only; absence == pending
  kind       TEXT NOT NULL CHECK (kind IN ('identity','worth','synthetic_gate')),
  target     TEXT NOT NULL,                   -- identity: links.pub | worth: person/parent id | synthetic_gate: synth- pub
  value      TEXT NOT NULL,                   -- identity: verify|detach|exclude|retarget|reject; worth/gate: yes|no
  source     TEXT NOT NULL,                   -- deep-context-review | user-guidance | judge-auto | sibling-settle | legacy-import
  note       TEXT NOT NULL DEFAULT '',        -- user_worth_note / skip note
  member_ids TEXT NOT NULL DEFAULT '[]',      -- worth only: worth_person_ids (membership survives reclustering)
  decided_at TEXT NOT NULL,
  PRIMARY KEY (kind, target)
);

-- The queue: a person with any unsettled link and no terminal person decision.
CREATE VIEW queue AS
SELECT p.person_id FROM people p
WHERE EXISTS (SELECT 1 FROM links l WHERE l.person_id = p.person_id
              AND NOT EXISTS (SELECT 1 FROM decisions d
                              WHERE d.kind = 'identity' AND d.target = l.pub))
  AND NOT EXISTS (SELECT 1 FROM decisions d
                  WHERE d.kind = 'worth' AND d.value = 'no'
                  AND (d.target = p.person_id
                       OR EXISTS (SELECT 1 FROM json_each(d.member_ids) m
                                  WHERE m.value = p.person_id)));
```

Every `OVERRIDE_COLUMNS` field and both `synthetic-people.csv` gates map to
exactly one home:

| review.csv column | new home |
|---|---|
| `public_identifier` | namespace split: `links.pub` / `people.person_id` / `decisions.target` (`parent-worth:` prefix dropped, kind=`worth`) |
| `worth_person_ids` | `decisions.member_ids` (kind=`worth`) |
| `action` | machine proposal -> `links.proposed_action`; settled outcome -> `decisions.value` |
| `approved` | dissolved: `''`=no decisions row; `auto`=row with machine source; `yes`=row with human source; `no`=row with `value='reject'` |
| `new_linkedin_url` / `new_public_identifier` | `links.new_url` / `links.new_pub` |
| `linkedin_url` | `links.url` |
| `match_emails` / `match_phones` | `links.match_emails` / `links.match_phones` |
| `confidence` / `reason` | `links.confidence` / `links.reason` |
| `person_id` | `links.person_id` (FK) |
| `source` / `updated_at` | `decisions.source` / `decisions.decided_at` (machine rows: `links.updated_at`) |
| `llm_reject` / `_confidence` / `_reason` | `links.reject` / `reject_confidence` / `reject_reason` |
| `llm_judge_fingerprint` | `links.judge_fingerprint` (paid cache key — copied verbatim) |
| `llm_worth` / `llm_worth_reason` | `people.llm_worth` / `people.llm_worth_reason` |
| `network_worth` / `user_worth_note` | `decisions` kind=`worth` `value` / `note` |
| synthetic-people.csv `approved` | `decisions` kind=`synthetic_gate` (profile columns stay in the CSV) |

**"Decide person" is one transaction** — replaces `/decide`'s per-row rewrite
fan-out (clicked row + real siblings + synthetic gates + pruned pubs):

```sql
BEGIN IMMEDIATE;
INSERT INTO decisions(kind,target,value,source,note,decided_at)
  VALUES ('identity', :pub, :value, 'deep-context-review', :note, :now)
  ON CONFLICT(kind,target) DO UPDATE SET value=excluded.value,
    source=excluded.source, note=excluded.note, decided_at=excluded.decided_at;
INSERT OR IGNORE INTO decisions(kind,target,value,source,note,decided_at)   -- sibling withdrawal
  SELECT 'identity', l.pub, 'detach', 'sibling-settle', '', :now
  FROM links l WHERE l.person_id IN (/* parent person_ids */) AND l.pub != :pub;
INSERT OR IGNORE INTO decisions(kind,target,value,source,note,decided_at)   -- folded + pruned synthetic gates
  VALUES ('synthetic_gate', :synth_pub, 'no', 'sibling-settle', '', :now);
COMMIT;
```

`INSERT OR IGNORE` + the `(kind, target)` PK is the whole idempotence story: a
prior human decision is a row, so it can never be overwritten by a settle sweep.
A crash mid-decision rolls back — no half-decided residue is representable.

## 3. Boundary contract (phase 1)

`review.sqlite` is the live store for **review_web only**. Everything outside
keeps its CSV contract; an export function writes `review.csv`-compatible rows
(exact `OVERRIDE_COLUMNS`, sorted keys — byte-stable against
`write_override_rows` output) at each decision commit and at stage exit.

| Surface | Phase-1 contract |
|---|---|
| review_web server (`server.py`, `model.py`, `decisions.py`, `retarget_queue.py`) | reads/writes `review.sqlite` only |
| `reconcile_linkedin`, `reconcile_deep_research`, `mirror_facts_worth` | keep **writing** `review.csv`; absorbed by import-on-serve (§4) |
| `apply_retargets`, `persist_review_identities`, `enrichment_contract`, `worth_view` CLI readers | keep **reading** the exported `review.csv` — unchanged |
| fan-in merge (`imports/merge_people.py`) | unchanged — already reads `directory.csv` (`deep_context_review` rows), never `review.csv` |
| `assemble_synthetic_profile` / `synthetic-people.csv` | profile columns stay CSV (it remains that file's writer); only the `approved` gate mirrors through `decisions`, and export writes the gate column back keyed by pub |

Nothing outside review_web changes in phase 1; the CSV remains the declared
pipeline artifact and the baton for the next process.

## 4. Migration

On serve: **if `review.csv` is newer than `review.sqlite` -> import.** Import is
idempotent and keyed (namespace split on `public_identifier`, upsert by key), so
a spurious trigger is harmless — no mtime bookkeeping beyond the comparison.
The legacy scrubs run **once, against the CSV, before import**; after import the
store is current-shape by construction and these shipped workarounds become dead
code:

| Workaround (file:function) | Why it dies |
|---|---|
| `common/legacy.py:resolve_stored_identity_policy` (all 4 rules + `server.py:cmd_serve` call site) | run once at import; rule 4's shape (half-decided parent) is unrepresentable post-transaction |
| `common/legacy.py:message_linkedin_aliases` (+ `worth_view` call site) | key aliases rewritten once at import |
| `review_web/server.py:review_rows_now` (`cached_rows`/`cached_rows_mtime`) | WAL reads are consistent; no other-writer mtime sniffing |
| `deep_context/common.py:acquire_review_session_lock` + `ensure_no_review_session` | `BEGIN IMMEDIATE` + `busy_timeout` is the writer lock; single-writer-session becomes per-transaction |
| `review_web/retarget_queue.py:run_guided_retarget` blank-and-restore (`blanked` dict + crash-restore block) | blank + judge + settle is one transaction; failure = rollback |
| `review_store.py:load_override_rows`/`write_override_rows` whole-file rewrites (review_web call sites) | keyed upserts; the pair survives only inside export/import |
| `review_web/decisions.py:apply_synthetic_decision` full-CSV rewrite per gate flip | gate is a `decisions` row; CSV gate column written at export |

Rollback at any point: delete `review.sqlite` — export kept `review.csv` fresh,
so the CSV code path (or an older release) resumes losslessly.

## 5. Plan

Behavior lock throughout: the same A/B gate used this week — build the queue on
the **jake-srv-new mirror copy** under old and new code, hash the ordered
(slug, state) list, require equality; any export diff explained line-by-line.

- [x] **P0 schema + import/export** (2026-08-05): `ReviewDb` in `review_db.py`; strict typed import; export through the CSV writer pair. Gate PASSED: byte-identical round-trip on arthur (758 links / 534 parents / 378 decisions / 14 gates) and the jake mirror (10425 / 6466 / 5328 / 10); zero unrepresentable rows — no scrub needed before import on either store.
- [ ] **P1 read path**: `build_parents`/model read via `ReviewDb`; all writes still CSV. Gate: A/B queue hash identical (CSV read vs sqlite read).
- [ ] **P2 write path**: `/decide`, `/worth`, retarget settle, judge-apply as transactions; export at decision commit + stage exit. Gate: mirror replay of a recorded decision log; final export diff = 0.
- [ ] **P3 delete workarounds** (table in §4). Gate: full unittest suite + one real staged run against local data, outputs diffed vs previous run.
- [ ] **P4 (out of scope here)**: outside writers move onto `ReviewDb`; CSV becomes export-only.

## 6. Risks / open questions

| Risk / question | Position |
|---|---|
| Concurrent CLI writers (reconcile/synthesis mid-session) | P1: unchanged (flock still refuses; import absorbs CSV writes at next serve). P3+: WAL + `busy_timeout` + `BEGIN IMMEDIATE`; contract relaxes from session-lock to per-transaction |
| `.bkup` story for a binary file | never rename a live WAL db (orphans `-wal`/`-shm`); use `sqlite3` `conn.backup()` -> `review.sqlite.bkup` before import, and the CSV export is the greppable archive. `wal_checkpoint(TRUNCATE)` on clean server exit so the file is self-contained |
| Powerset upload / feedback paths | feedback composes identifiers from the in-memory model, not the file; nothing uploads `review.csv` today — re-grep before P2 |
| `$clean-slate` / `$deep-context restart` | `review.sqlite` holds human decisions -> joins clean-slate's preserve/backup list; `restart` becomes `DELETE FROM decisions WHERE source IN (human sources)` + export |
| Install-base migration (Jake's machine) | first serve post-update imports with scrubs; rehearse on the jake-srv-new mirror first; rollback = delete `review.sqlite` (§4) |
| Pipeline declared contract | `review.csv` stays the declared artifact (export preserves `ReviewRow`); whether `review.sqlite` needs its own `Artifact` entry — open, likely declaration-only |
| `json_each` availability | bundled sqlite3 has JSON1 everywhere we ship; if a floor machine lacks it, membership check moves app-side — verify in P0 |
| Worth membership vs reclustering | `member_ids` mirrors `worth_person_ids` exactly; the queue view honors it, matching `has_human_worth` — locked by the P1 queue-hash gate |
