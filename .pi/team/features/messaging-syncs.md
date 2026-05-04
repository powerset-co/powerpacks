# Feature Owner: Powerpacks — Messaging Syncs

## Mission
Own reusable messaging sync powerpacks for iMessage and WhatsApp.

## Primary scope

```txt
packs/messages/
primitives/
scripts/smoke-messages.sh
tests/test_messages_pack.py
tests/test_whatsapp_primitives.py
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
uv run pytest tests/test_messages_pack.py tests/test_whatsapp_primitives.py
bash scripts/smoke-messages.sh
```

## Startup checklist

1. Read this dossier and `.pi/team/manifest.yaml`.
2. Read `packs/messages/README.md` and relevant primitive docs.
3. Summarize the current messaging pack contract before editing.
