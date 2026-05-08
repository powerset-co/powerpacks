# extract_imessage_contacts

Extract iMessage/SMS relationship metadata from local macOS SQLite databases.

This primitive is intentionally stdlib-only:

- no Homebrew
- no pip dependencies
- no `contact-exporter`
- no message content reads

It reads `~/Library/Messages/chat.db` in SQLite read-only mode and uses local
AddressBook SQLite databases for phone inventory and phone-to-name lookup. The
default export includes Contacts.app phone rows even when they do not have
iMessage history, so it matches the one-step contact import experience.

Examples:

```bash
python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py check

python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py open-privacy-settings --target both

python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py extract \
  --output-csv .powerpacks/messages/imessage.contacts.csv \
  --output-jsonl .powerpacks/messages/imessage.contacts.jsonl
```

`open-privacy-settings` is macOS-only. Use `--target full-disk-access` for
Messages `chat.db` access, `--target contacts` for AddressBook name matching,
or `--target both`.

If permissions or schema assumptions fail, the primitive writes a manifest with
diagnostics so the harness can continue and an agent can patch the primitive.
