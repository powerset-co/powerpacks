# messages_harness

Run messages-pack primitives as a tolerant harness.

For iMessage, the harness runs:

1. `extract_imessage_contacts check`
2. `extract_imessage_contacts extract`

It records each subprocess result and writes a run manifest. If a step fails, it
keeps the manifest and emits a repair note instead of hiding the failure behind
a generic exception.

Example:

```bash
python packs/messages/primitives/messages_harness/messages_harness.py imessage \
  --output-dir .powerpacks/messages
```
