---
name: import-messages
description: Import local iMessage and WhatsApp relationship signals through the Powerpacks messages pack. Use when the user wants to extract, normalize, review, or upload contacts from local messaging apps.
---

# Import Messages

Use this skill for local message-contact imports.

The pack is privacy-first:

- never request or store message content
- only work with contact metadata: phone, name, source, groups, message counts,
  last message timestamp, skip/review/match metadata
- do not run extraction or upload unless the user explicitly asks for that
  action in the current task
- prefer bare Powerpacks primitives for iMessage
- use `powerset-contacts` / `contact-exporter` only as the compatibility
  backend for surfaces not yet ported, such as WhatsApp or upload

## Workflow

1. Inspect `packs/messages/docs/harness.md`.
2. Check local iMessage access:
   `python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py check`
3. After explicit user approval, run:
   `python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py extract --output-csv .powerpacks/messages/imessage.contacts.csv --output-jsonl .powerpacks/messages/imessage.contacts.jsonl`
4. Present the manifest counts and artifact paths. If the primitive fails,
   inspect the manifest diagnostics and patch or rerun the primitive with
   explicit paths.
5. For a tolerant run that records repair notes, use:
   `python packs/messages/primitives/messages_harness/messages_harness.py imessage --output-dir .powerpacks/messages`
6. Upload only after a separate
   explicit upload request.

## Channel Notes

- iMessage is the easiest first channel on macOS because it is just read-only
  SQLite access to Messages and Contacts metadata.
- WhatsApp depends on Docker and WAHA. Treat it as an interactive local session
  with QR auth and likely user involvement.
- `powerset_contacts_harness` is available for compatibility with
  `contact-exporter` while WhatsApp/upload are still being ported.
