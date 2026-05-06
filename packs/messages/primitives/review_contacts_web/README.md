# review_contacts_web

Local-only CSV review editor for `.powerpacks/messages/contacts.csv`.

```bash
python packs/messages/primitives/review_contacts_web/review_contacts_web.py serve \
  --contacts .powerpacks/messages/contacts.csv \
  --open
```

The server binds to `127.0.0.1`, edits the CSV in place, and never uploads
data. It is intended to replace spreadsheet/TUI cleanup for skip and match
fields.
