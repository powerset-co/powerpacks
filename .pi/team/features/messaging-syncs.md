# Feature Owner: Powerpacks — Messaging Syncs

## Mission
Own reusable messaging sync powerpacks for iMessage and WhatsApp.

## Primary scope

```txt
packs/ingestion/skills/import-messages/
packs/ingestion/primitives/{discover_contacts_pipeline/messages/,import_contacts_pipeline/messages/}
packs/ingestion/primitives/discover_contacts_pipeline/messages/{extract_imessage,whatsapp_wacli,normalize_contacts,merge_contacts}.py
tests/test_messages_pack.py
tests/test_whatsapp_wacli.py
tests/test_ingestion_messages_contract.py
```

## Responsibilities

- iMessage skill/powerpack coverage
- WhatsApp skill/powerpack coverage
- message/contact sync smoke tests
- privacy and secret-safety invariants for messaging examples
- reusable docs and workflows for app repos that consume messaging syncs

## Invariants

- Do not inspect, print, commit, or exfiltrate private raw messages/contact exports.
- Treat tokens, phone numbers, and message content as sensitive.
- Prefer fixture-based tests and smoke harnesses over live account access.
- Ask before external API writes or sync replays.

## Regression checks

```bash
uv run --project . python -m unittest \
  tests.test_messages_pack \
  tests.test_whatsapp_wacli \
  tests.test_ingestion_messages_contract
```

## Startup checklist

1. Read this dossier and `.pi/team/manifest.yaml`.
2. Read `packs/ingestion/docs/message-import-pipeline.md` and the relevant
   ingestion skill/primitive docs.
3. Summarize the current message-ingestion contract before editing.
