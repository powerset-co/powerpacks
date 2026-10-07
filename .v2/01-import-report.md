# Block 01 — import load: report

Created: 2026-10-06. Change log: 2026-10-06 first pass.

## Files
- `packs/ingestion/primitives/deep_context_v2/import_load/load.py`: 136 lines (`ImportLoad(Node)`, `name="import_load"`, `reads=()`, the six declared writes; `main`)
- `packs/ingestion/primitives/deep_context_v2/import_load/__init__.py`: 1 line

## From v1
- Imported: `common/contact_fields.py` `normalize_email`, `normalize_phone`, `is_shared_mailbox`; `packs/shared/csv_io.py` `CsvIO.read_dict_rows`.
- Copied: the shared-mailbox predicate `deep_context/ensure_parents/imported_people.py:165-175` (that module imports the v1 `Db` at the top, so it is not importable), and the owner match `deep_context/db/projectors.py:73-79` (normalize the owner's emails/phones, intersect).

## Dropped (v1 file:line — reason)
- `imported_people.py:171-174` `SourceChannel.LINKEDIN not in source_channels`: v2 candidates never carry linkedin_csv (no `SourceChannel` member; spec: "Rows join only on identical id"). On this root 0 of the 2 connection emails are role addresses, so it changes nothing here.
- `imported_people.py:127-136` lowercasing ids, rejecting `/` `\` ids, grouping duplicate ids: ids are unique within each CSV and disjoint across them (email vs phone).
- `imported_people.py:139` `merge_group` of duplicate ids: same reason.
- `imported_people.py:141-143` display_name fallback to first+last: 0 rows on either CSV have parts without full_name; DDL says display_name is '' when names conflict. display_name = full_name.
- `imported_people.py:121-124` `_channels` JSON-list branch: every CSV writes comma-joined channels; `SourceChannel(...)` is the validation.
- `imported_people.py:103-108, 150` superseded_person_ids: re-import machinery, spec deletes it.
- `imported_people.py:111-119, 151-159` public_identifier, linkedin_url, avatar, title, company, location, headline, counts on the person: not v2 candidate columns; the whole row lives in `import_json`.
- `imported_people.py:182-183` missing-file `return ()`: `required_files()` covers it.
- `imported_people.py:185-187` primary_phone fallback from phone/phone_e164: those columns are not in the PeopleRow CSVs.
- `imported_people.py:188` `PeopleRow.model_validate`: the CSV row is stored as-is; no spec rule needs pydantic coercion.
- `imported_people.py:192-206` `stored_imported_people`, `_components`: parent regroup; spec deletes it.
- `imported_people.py:209-376` the whole SQLite projection: prior-row merge, parent mint, ParentRow, slugs, is_ghost/facts carry, the union with prior identifiers/sources, LinkRow/candidate link rows, `replace_imported_people`. Replaced by upsert + per-candidate child rewrite + the connections lookup.
- `imported_people.py:316` skip when the normalized value is empty: importers emit normalized values; on this root every display_value equals its normalized value (602/602).
- `contact_fields.py:142-172` `emails_from_row`/`phones_from_row` (extra columns email/handle/emails/phone/phone_e164/phones, regex extraction, lowercasing): the brief limits identifiers to primary_*/all_*, and display_value must be the CSV value.
- `projectors.py:71-72` owner-None return and `:80-81` skip-already-flagged: `load_owner` runs first in the same block; is_owner is written on every upsert.

## Run
`uv run --no-sync --project . python -m packs.ingestion.primitives.deep_context_v2.import_load.load --data-root /Users/arthur/workspace/powerpacks/.powerpacks`

```
completed candidates=602 names=613 connections=277 shared_mailboxes_dropped=37 owner_flagged=7
source_gmail_msgvault=523 source_imessage=71 source_whatsapp=22 identifiers_email=523 identifiers_phone=79
```
A second run printed identical counts (idempotent). Manifest: `.powerpacks/deep-context/v2-manifests/import_load.json`, status completed.

## Checks (read-only, v1 store opened `?mode=ro`)
- 639 CSV rows (560 Gmail + 79 messages) − 37 shared mailboxes = 602 candidates. All 37 dropped are Gmail.
- Every v2 candidate id is in the v1 roster (0 v2-only). The v1 roster has 647 `candidate:` ids; the 45 not in v2 are 35 shared mailboxes v1 carried forward from before its 2026-10-01 drop, plus 10 ids (2 email, 8 phone) absent from today's CSVs (v1 carry-forward of earlier imports).
- Every identifier satisfies `candidate_id = 'candidate:' || kind || ':' || normalized_value` (602/602); one identifier per candidate on this root.
- 35 candidates have display_name '' (Gmail rows with no name); 0 parent rows; 277 connections, 2 with email, 0 empty position.
- owner_flagged 7 (all email ids), the same 7 v1 flags among `candidate:` ids. v1 `people.is_owner` = 10; the other 3 are non-candidate ids (LinkedIn/legacy rows), which v2 does not have.

## Spec/brief conflicts (verbatim)
- Spec, "The v2 rewrite plan": "Block 01 is the load only: input merged/people.csv and the fan-in manifest". The brief says "Do **not** read `merged/people.csv` ... or any manifest". I followed the brief: inputs are the three per-source CSVs.
- DDL comment: `display_name TEXT NOT NULL, -- '' when the source names conflict` (fan-in `name_conflicts`). Without the manifest, the only source of a conflict is the importer leaving full_name blank, and that is what the load stores. No fan-in conflict is possible across the two CSVs because their ids never overlap.
- `deep_context_v2/__init__.py` says "nothing here imports from it at runtime" (v1). The brief says to reuse v1 by importing pure modules, so this block imports `common/contact_fields.py` and `shared/csv_io.py`. Not changed: it is a foundation file.

## Fixed after Astra review (2026-10-06, orchestrator)
- connections.position reads current_title (the importer's position column); the headline fallback is gone.
