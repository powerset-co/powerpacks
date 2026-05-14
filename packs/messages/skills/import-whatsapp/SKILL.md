---
name: import-whatsapp
description: Isolated WhatsApp metadata import flow using openclaw/wacli instead of WAHA.
---

# Import WhatsApp

Use this skill for `$import-whatsapp` or when the user asks to sync WhatsApp
through `openclaw/wacli` in isolation.

## Rule

When the user literally types `$import-whatsapp`, treat that command as explicit
consent to run the isolated WhatsApp flow in this turn. Do not ask another
consent question.

Run:

```bash
uv run --project . python packs/messages/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py run
```

This flow:

- installs `wacli` with Homebrew if it is missing
- uses `.powerpacks/messages/wacli` as an isolated local store
- authenticates with a WhatsApp QR scan if needed
- runs one bounded WhatsApp sync
- exports `.powerpacks/messages/wacli.contacts.csv`

The Powerpacks export reads metadata only: phone, name, source, group metadata,
direct-chat message counts, and timestamps. It does not read message bodies.

## Execution

Run the primitive directly in the main shell so QR/login output is visible.
Relay only user-facing lines that begin with `[import-whatsapp]`, with the
prefix removed. Do not stream raw JSON, local contact data, phone numbers,
message data, or terminal transcripts unless a failure needs diagnosis.

Expected user-facing status:

- `Installing WhatsApp sync helper.`
- `WhatsApp needs a QR scan.`
- `Refreshed WhatsApp QR page.`
- `Syncing WhatsApp Messages and Contacts.`
- `WhatsApp sync finished.`

If WhatsApp says it cannot link new devices right now, tell the user:

`WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-whatsapp.`

## Output

Be terse. On completion, report:

`Imported X WhatsApp contacts.`
