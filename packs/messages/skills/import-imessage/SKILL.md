---
name: import-imessage
description: Import local iMessage relationship signals through the Powerpacks messages pack.
---

# Import iMessage

Use this skill for local iMessage-only message-contact imports on macOS.

The pack is privacy-first:

- never request or store message content
- only work with contact metadata: phone, name, source, groups, message counts,
  last message timestamp, skip/review/match metadata
- do not run extraction or upload unless the user explicitly asks for that action
- keep message contact normalization as explicit steps the run can replay

## Prereqs

- macOS (this skill is macOS-only)
- Python 3.9+ (stdlib only, no extra packages)
- **Full Disk Access** granted to the terminal / IDE Codex or Claude Code is
  running from. Without it, `chat.db` reads will fail with permission errors.
  Open `System Settings → Privacy & Security → Full Disk Access`, add the app,
  and restart it.

If step 2 reports `chat_db.readable: false` or addressbook errors, that is
almost always a Full Disk Access issue. Do not retry blindly — surface the
diagnostic to the user and ask them to grant access.

## Workflow

1. Inspect `packs/messages/docs/harness.md`.
2. Check local iMessage access:
   `python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py check`
3. After explicit user approval, run:
   `python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py extract --output-csv .powerpacks/messages/imessage.contacts.csv --output-jsonl .powerpacks/messages/imessage.contacts.jsonl`
4. Normalize the exported rows to the canonical schema:
   `python packs/messages/primitives/normalize_message_contacts/normalize_message_contacts.py normalize --input .powerpacks/messages/imessage.contacts.csv --out-jsonl .powerpacks/messages/imessage.contacts.normalized.jsonl`
5. Merge into the unified `contacts.csv` (single-channel today, ready for WhatsApp later):
   `python packs/messages/primitives/merge_message_contacts/merge_message_contacts.py merge --input .powerpacks/messages/imessage.contacts.csv --output .powerpacks/messages/contacts.csv`
5. Present the manifest counts and artifact paths. If the primitive fails,
   inspect the manifest diagnostics and patch or rerun with explicit paths.
6. For a tolerant run that records repair notes, use:
   `python packs/messages/primitives/messages_harness/messages_harness.py imessage --output-dir .powerpacks/messages`
7. Upload only after a separate explicit upload request.
