# Messages Pack

`packs/messages` is a local-first import harness for relationship signals from
iMessage and WhatsApp.

The pack uses bare, inspectable primitives first. The current boundary is:

- keep iMessage extraction in a stdlib-only Python primitive
- keep `powerset-contacts` available as an optional compatibility backend for
  WhatsApp, matching, review, and upload
- let Powerpacks own task state, primitive contracts, schemas, normalization,
  manifests, and agent-facing workflow instructions
- let the harness capture local failures as repair artifacts so an agent can
  patch a primitive for the machine in front of it

## Primitive Surface

- `extract_imessage_contacts`: read local macOS Messages/Contacts SQLite
  metadata with Python stdlib only
- `messages_harness`: run message primitives tolerantly and emit repair notes
- `normalize_message_contacts`: convert `contact-exporter` CSV output into a
  canonical JSONL artifact and summary manifest
- `powerset_contacts_harness`: optional compatibility wrapper for
  `contact-exporter`

## Harness Stance

Extraction is local and consentful. The harness can prepare and record commands,
but an agent should not run iMessage, WhatsApp, or upload actions unless the user
has explicitly asked for that action in the current task.

Generated artifacts should live under `.powerpacks/messages/` by default.
