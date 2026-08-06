# Deep Context — stage spec (owner intent)

Created: 2026-08-06
Change log:
- 2026-08-06: strict stage sequence makes missing prerequisites absent from
  downstream views instead of inventing fallback state.
- 2026-08-06: worth-gated attached-link judging and self-heal in one upstream
  SQL queue; pinned-threshold machine acceptance now records at judge time.
- 2026-08-06: scaffold drafted by Claude, approved as-is by Arthur. This is
  the acceptance document: audits verify against these lines, not old behavior.

Rule this document creates: code is guilty until traced to a line here.
Anything in the package unmapped to a stage below is a deletion candidate.

---

## 0. import / fan-in  (upstream boundary, not deep-context)
Purpose: turn source exports (LinkedIn, Gmail, iMessage, WhatsApp, Twitter)
  into one merged people.csv with per-person identifiers + source channels.
Reads: source stores/exports.    Writes: network-import/merged/people.csv.
Decision: which source rows are the same person (deterministic identifier match).
Never: LLM calls, paid lookups, deep-context state.

## 1. ensure-parents  (entry projection)
Purpose: every imported person exists in SQLite with a stable parent id, once.
Reads: people.csv (input boundary), SQLite people/parents.
Writes: SQLite people/parents (get-or-create/absorb; ids minted once, immutable).
Decision: which existing parent a new child joins (shared identifier), else mint.
Never: re-derive an id from membership; batch rebuilds; LLM.

## 2. collect
Purpose: one raw message bundle per parent from all its identifiers, capped.
Reads: chat.db / wacli.db / msgvault.db (read-only), SQLite parents.
Writes: raw/<parent_id>.json (ephemeral, gitignored) + receipt.
Decision: none — mechanical crawl under the privacy policy.
Never: decisions from files; group bodies beyond the standing policy; network.

## 3. synthesize
Purpose: facts + a worth verdict per parent, only when evidence changed.
Reads: raw bundles, SQLite fingerprint cache.
Writes: facts (SQLite via projection), facts/<parent_id>.jsonl, receipt.
Decision: skip-or-spend per parent (fingerprint + SYNTHESIS_VERSION);
  adaptive stop (confidence 0.85 / saturation 2 / max 20 batches).
Never: estimate mutating anything; per-child fragment synthesis; re-billing
  unchanged evidence.

## 4. merge candidates  (same-human discovery)
Purpose: find two parents that are one human when no identifier links them.
Reads: SQLite parents/facts.    Writes: merge_verdicts (SQLite, paid pair cache).
Decision: propose same-person pairs (blocking + one pair judge, verdicts cached
  by pair + evidence fingerprint). Accepted verdict => one merge_parents
  transaction (survivor keeps id; newest human decision wins worth).
Never: whole-graph rebuilds; CSV verdict caches; mutating dossiers in place.

## 5. worth review  (human)
Purpose: you sweep the effective-Maybe queue; Yes/No are visible and editable.
Reads: SQLite worth queue views.    Writes: parents.human_worth* via decide_worth.
Decision: yours only. Machine default is the synthesis verdict.
Never: machine touching human_worth*; queue derived from anything but SQLite;
  an owner person never hides its family from review.

## 6. enrich  (research + THE judge)
Purpose: every effective-Yes parent ends with a verified LinkedIn or a
  synthetic profile; attached links on effective-Maybe parents are validated.
Reads: SQLite queues, cached profiles, dossier evidence.
Writes: research results + judge verdicts into SQLite; receipts.
Decision: (a) research iff no usable LinkedIn (Parallel → proposed URL +
  reasoning, else synthetic); (b) ONE judge scores any candidate URL against
  the parent's whole evidence → confidence; one threshold table. Attached-link
  judging and self-heal skip effective_worth = 'no' through one SQL queue
  predicate upstream of every paid call; research keeps the stricter
  effective_worth = 'yes' gate. Machine verdicts that clear the pinned
  threshold table auto-apply into machine decision columns at judge time; a
  human decision always wins; the review queue is the below-threshold slice.
Never: a second judge/evidence/bio composition; spend without flag + estimate;
  re-billing a person already researched (one handle, one cache); paid work on
  effective_worth = 'no'.

## 7. linkedin review  (human)
Purpose: you settle pending candidate families: verify / retarget / skip.
Reads: SQLite linkedin queue views.    Writes: links.decision_* via decide_identity.
Decision: yours only; one winner per parent, siblings auto-settle.
Never: machine writing decision_*; fan-out child updates (resolve via join).

## 8. realize / index
Purpose: project the approved network to people.csv/directory.csv and build
  the search index.
Reads: SQLite.    Writes: export CSVs (re-derivable), search index.
Decision: none — pure projection of decided state.
Never: exports feeding back as inputs; identity logic at export time.

---

## Cross-cutting (applies to every stage)
1. SQLite is the record; every file is cache, receipt, or re-derivable export.
2. manifest.json is a write-only receipt.
3. Paid work is fingerprint-keyed, never name-keyed.
4. Spend gates are flags + estimates; dry-run mutates nothing.
5. Human columns are machine-untouchable.
6. Workflow next-action is queue-derived; no stage state machine.
7. Privacy: bodies read only where the standing policy allows; dossiers store
   synthesized claims; committed artifacts use synthetic identities.
8. Strict sequence, best-case assumption: each stage assumes its prerequisites
   ran; a row missing a prerequisite is absent from downstream views and queues
   — never defaulted, coalesced, or guessed into visibility. The only sanctioned
   coalesce is precedence: a human decision beats a machine verdict.

## 9. migration  (sanctioned legacy, dying)
Purpose: absorb a pre-SQLite install exactly once, preserving paid artifacts
  and human decisions.
Reads: legacy files (index.json, review.csv, facts, verdicts, research).
Writes: SQLite.    Detection: check-readiness routes to migrate-sqlite.
Removal condition: delete legacy.py, migration-only graph machinery, and
  parent_identity_proof once no install predates the migration.
