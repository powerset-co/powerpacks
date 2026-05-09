# merge_message_contacts

Merge N per-channel message-contact CSVs into a single canonical
`contacts.csv`. Stdlib-only.

Schema reference: `packs/messages/schemas/contacts-csv.md`.
If an input CSV uses legacy headers such as `phone_e164`, `display_name`, or
`total_messages`, convert it to the canonical contacts schema before rerunning.
The primitive fails fast with the schema path instead of silently writing an
empty merge.

## Usage

```bash
# Merge iMessage + WhatsApp into one contacts.csv.
python packs/messages/primitives/merge_message_contacts/merge_message_contacts.py merge \
  --input .powerpacks/messages/imessage.contacts.csv \
  --input .powerpacks/messages/whatsapp.contacts.csv \
  --output .powerpacks/messages/contacts.csv

# Single input is also valid (lets you alias to contacts.csv before WhatsApp lands).
python packs/messages/primitives/merge_message_contacts/merge_message_contacts.py merge \
  --input .powerpacks/messages/imessage.contacts.csv \
  --output .powerpacks/messages/contacts.csv
```

## Merge rules per phone

| Field | Rule |
| --- | --- |
| `phone` | Canonicalized first |
| `name` | First non-empty name across inputs (later inputs do not overwrite) |
| `source` | Comma-joined unique sources, first-seen order |
| `is_in_group_chats` | Logical OR |
| `group_names` | Union, sorted case-insensitively, ` \| ` joined |
| `message_count` | Total across known per-channel counts |
| `imessage_message_count`, `whatsapp_message_count` | Per-channel counts; later rows for the same phone+channel overwrite earlier rows |
| `last_message` | Maximum ISO timestamp |
| `imessage_last_message`, `whatsapp_last_message` | Per-channel latest timestamp; later rows for the same phone+channel overwrite earlier rows |
| `skip` | Logical OR |
| `match_*` | Highest `match_confidence` wins; tie-breaker `matched > suggested > unmatched > empty` |

## Input schema

Minimal accepted input columns:

```text
phone,name
```

Canonical output header:

```text
phone,name,source,is_in_group_chats,group_names,message_count,imessage_message_count,whatsapp_message_count,last_message,imessage_last_message,whatsapp_last_message,skip,match_status,matched_person_id,matched_name,matched_linkedin_url,match_confidence,match_method,match_reason
```

## Output

- The unified `contacts.csv` is sorted by `(message_count desc, last_message
  desc, phone)`.
- A manifest JSON is written next to it with per-input row counts, the number
  of cross-channel (multi-source) phones, and a `by_source` histogram.
