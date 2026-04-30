# normalize_message_contacts

Normalize a `contact-exporter` CSV into canonical Powerpacks messages JSONL and
a manifest.

Example:

```bash
python packs/messages/primitives/normalize_message_contacts/normalize_message_contacts.py normalize \
  --input .powerpacks/messages/contacts.csv \
  --out-jsonl .powerpacks/messages/contacts.normalized.jsonl
```
