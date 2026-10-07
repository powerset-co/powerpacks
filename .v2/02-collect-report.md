# Block 02 Collect: report

Created: 2026-10-06. Change log: 2026-10-06 first pass.

## Files
- `packs/ingestion/primitives/deep_context_v2/collect/collect.py`: 144 lines (55 of them the docstring: From v1, copied lines, dropped branches)
- `packs/ingestion/primitives/deep_context_v2/collect/__init__.py`: empty

## From v1 (imported, not copied)
- `deep_context.collection.context_sources`: ContextSources, CHAT_MESSAGE_CAP (1600), DEFAULT_WACLI_DB, gni.MsgvaultStore
- `deep_context.collection.models`: CollectionBundle, MessageChannel
- `deep_context.shared.common`: Person
- `common.paths.DEFAULT_MSGVAULT_DB`, `discover.messages.extract_imessage.DEFAULT_CHAT_DB`

## v1 lines copied
- `db/context_queries.py:140-171` collection_sources: became `_people`, reading candidates / candidate_identifiers / candidate_sources (is_owner = 0, sorted normalized values)
- `collection/collect_person_context.py:75-81, 114-143`: ContextSources construction and the per-candidate loop
- `db/projectors.py:196` + `common/jsonio.py:100`: payload_json = `json.dumps(payload, sort_keys=True, separators=(",", ":"))`, which equals v1's stored bytes (the file was sort_keys, then re-dumped compact)
- `db/projectors.py:194` + `common/jsonio.py:100`: content_fingerprint = sha256 of `json.dumps(payload, indent=2, sort_keys=True) + "\n"`, v1's file-bytes hash

## Dropped branches
- `context_queries.py:147` `kind IN ('email','phone')`: the DDL CHECK allows only those
- `context_queries.py:148-152` EXISTS message-source filter: v2 sources are message channels only
- `collect_person_context.py:96,116-120` person_histories / processed-hash exclusion: carry-forward
- `collect_person_context.py:121-122` `if not messages and history: continue`: prior-run skip
- `collect_person_context.py:125-126` `if not messages and not groups`: now `if not messages` (no messages, no bundle). A v1 groups-only bundle gets no row
- `collect_person_context.py:134-135` dry_run
- `collect_person_context.py:144-145` raw/<id>.json + project_person_source_bundle: the store is the record
- `collect_person_context.py:146-147` progress print; `113,148-149` try/finally around close
- `collect_person_context.py:151-152` normalize_cached_bundles (parent bundles): per candidate only
- `collect_person_context.py:153-189` group-body count, privacy receipt, timings, manifest class: Node.run writes the manifest
- `collect_person_context.py:201-216` path and tuning flags: paths come from `--data-root`, tuning stays at v1's defaults

## Run
`uv run --no-sync --project . python -m packs.ingestion.primitives.deep_context_v2.collect.collect --data-root /Users/arthur/workspace/powerpacks/.powerpacks`
(took 24 s; block 01's import_load manifest said completed with 602 candidates, 7 of them the owner)
```
completed candidates=595 bundles=591 no_messages=4 capped=186 messages_gmail=16421
messages_imessage=8027 messages_imessage_group=15451 messages_whatsapp=1508
```
- 595 = 602 − 7 owner. 591 rows, 591 distinct fingerprints, 0 owner rows. Manifest: `.powerpacks/deep-context/v2-manifests/collect.json`.

## Compared with v1 (`artifacts kind='source_bundle'`, opened read-only)
- v1 keeps 647 people under `candidate:` ids. 463 of the 591 v2 bundles have a v1 bundle with the identical person_id. The other 128 have no v1 person-level source_bundle, under the same id or under any v1 person with the same identifier. I haven't checked why.
- **420/463 have byte-equal payload_json and an equal content_fingerprint.**
- 43 differ. First differing key: `full_name` in 28 (block 01's display_name is not v1's people.display_name, so that comes from the import, not collect) and `messages` in 15.
- Message fingerprint sets are equal in 446/463, and in 301/307 where both sides are uncapped. The 6 uncapped mismatches all have more messages in v2 and are explained:
  - 4 are messages dated 2026-10-05/06, which arrived after v1 collected (04:16Z).
  - 2 are imessage_group bundles where v1 shrank to 15 and 78 messages: v1 excludes already-synthesized messages before capping. That exclusion is the carry-forward the spec drops; v2 has 731 and 1600.
- The 3 capped `messages` mismatches shown (v1 1/1/2 messages, v2 10/11/45, same messages_available) are the same exclusion.

## Could not satisfy / deviations
- **Transitive v1 db imports.** Importing `context_sources` (mandated) loads `deep_context.db.store`, `db.context_queries`, `db.queries`, `db.schema` and `db.identity_policy` at import time, through `discover.gmail.msgvault.store → msgvault.sync → merge_candidates → synthesis → db.*`. `shared.common` loads `db.models`. No v1 Db is opened or called. Breaking that chain means editing v1, which is out of scope.
- **required_files().** The brief says "where v1 declares them required", but v1 declares none required (`required=False` at `collect_person_context.py:46-49`). I declared all three stores (msgvault.db, chat.db, `<data-root>/messages/wacli/wacli.db`), so a missing store means not_ready.
- **chat.db readability.** chat.db was readable from this process (177,237 messages), so not_ready did not come up. A chat.db that exists but can't be read (no Full Disk Access) still passes `exists()`, so the run would fail with v1's "Cannot read …" error rather than report not_ready.
- **`owner` in reads.** It is declared as the brief says but never read: v1 decides direction from msgvault `account_emails()`. The spec's block contract lists reads without `owner`.
- **Key order.** The DDL comment says "CollectionBundle, pinned key order". v1's stored bytes have sorted keys (from the file round trip), so v2 matches that, not `to_payload()` order.

## Fixed after Astra review (2026-10-06, orchestrator)
- `owner` removed from `reads` (never read). No `required_files`: a missing store is a channel with no messages, as v1's readers already treat it.
- content_fingerprint is the sha256 of the stored payload_json; the pretty-printed re-serialization for v1's file hash is gone.
