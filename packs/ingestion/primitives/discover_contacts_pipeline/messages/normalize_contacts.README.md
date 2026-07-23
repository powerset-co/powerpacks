# messages/normalize_contacts.py

Normalize a message-contact CSV into canonical Powerpacks Messages JSONL and a
manifest.

Example:

```bash
python packs/ingestion/primitives/discover_contacts_pipeline/messages/normalize_contacts.py normalize \
  --input .powerpacks/messages/contacts.csv \
  --out-jsonl .powerpacks/messages/contacts.normalized.jsonl
```
