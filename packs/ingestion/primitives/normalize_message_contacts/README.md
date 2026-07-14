# normalize_message_contacts

Normalize a message-contact CSV into canonical Powerpacks Messages JSONL and a
manifest.

Example:

```bash
python packs/ingestion/primitives/normalize_message_contacts/normalize_message_contacts.py normalize \
  --input .powerpacks/messages/contacts.csv \
  --out-jsonl .powerpacks/messages/contacts.normalized.jsonl
```
