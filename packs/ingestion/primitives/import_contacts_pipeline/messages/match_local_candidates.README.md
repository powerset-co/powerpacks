# match_local_candidates

Local identity matcher between a message-contacts CSV and already imported
people. The canonical `$import-messages` flow supplies Gmail and LinkedIn
`people.csv` rows through `--local-people`; no external candidate catalog is
loaded by default. Stdlib-only.

## Usage

```bash
python packs/ingestion/primitives/import_contacts_pipeline/messages/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv \
  --local-people .powerpacks/messages/_local_people.csv
```

`--candidates /path/to/candidates.csv` is an explicit opt-in for standalone or
legacy callers. Omitting it never reads a repo-root `powerset_contacts.csv`.

The contacts CSV is updated in place: `match_status / matched_person_id /
matched_name / matched_linkedin_url / match_confidence / match_method /
match_reason` columns are populated.

## Match tiers

0. Unique exact phone or email match → `matched`, confidence `1.0`, when prior
   review state allows it; otherwise it remains `suggested` until review
1. Single exact normalized-name match → `matched`, confidence `1.0`
2. Multiple exact normalized-name matches → `suggested`, confidence `0.80`
3. Single-token first-name-only match → `suggested`, never automatically matched
4. Same-last-name pool, unique first-name prefix candidate → `matched`,
   confidence `max(0.95, fuzzy ratio)`
5. Same-last-name pool, multiple prefix candidates → `suggested` (best score)
6. Same-last-name pool, fuzzy ratio ≥ `0.94` and margin ≥ `0.05` → `matched`
7. Fuzzy ratio ≥ `0.80` → `suggested`
8. Otherwise → `unmatched`

When an earlier research review exists, no matching tier can silently expand
the user's approved set: unapproved or unseen contacts are demoted to
`suggested` and must pass through review again.

## Output

A manifest JSON next to the contacts CSV with `stats: {total, matched,
suggested, unmatched}`.
