# match_local_candidates

Local name matcher between a message-contacts CSV and the Powerset candidate
CSV produced by `sync_powerset_candidates`. Stdlib-only.

## Usage

```bash
python packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv \
  --candidates .powerpacks/messages/powerset_contacts.csv
```

The contacts CSV is updated in place: `match_status / matched_person_id /
matched_name / matched_linkedin_url / match_confidence / match_method /
match_reason` columns are populated.

## Match tiers

1. Single exact normalized-name match → `matched`, confidence `1.0`
2. Multiple exact normalized-name matches → `suggested`, confidence `0.80`
3. Same-last-name pool, unique first-name prefix candidate → `matched`,
   confidence `max(0.95, fuzzy ratio)`
4. Same-last-name pool, multiple prefix candidates → `suggested` (best score)
5. Same-last-name pool, fuzzy ratio ≥ `0.94` and margin ≥ `0.05` → `matched`
6. Fuzzy ratio ≥ `0.80` → `suggested`
7. Otherwise → `unmatched`

## Output

A manifest JSON next to the contacts CSV with `stats: {total, matched,
suggested, unmatched}`.
