# merge_message_contacts

Merge N per-channel message-contact CSVs into a single canonical
`contacts.csv`. Stdlib-only.

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
| `message_count` | Maximum across inputs (matches `normalize_message_contacts`) |
| `last_message` | Maximum ISO timestamp |
| `skip` | Logical OR |
| `match_*` | Highest `match_confidence` wins; tie-breaker `matched > suggested > unmatched > empty` |

## Output

- The unified `contacts.csv` is sorted by `(message_count desc, last_message
  desc, phone)`.
- A manifest JSON is written next to it with per-input row counts, the number
  of cross-channel (multi-source) phones, and a `by_source` histogram.
