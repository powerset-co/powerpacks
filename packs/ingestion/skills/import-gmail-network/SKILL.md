---
name: import-gmail-network
description: Gmail network import from local msgvault metadata. Uses msgvault SQLite, writes .powerpacks artifacts, and avoids Powerset-hosted Gmail OAuth/sync.
---

# Import Gmail Network

Use this skill when the user wants to import Gmail/network contacts into
Powerpacks.

V1 scope is intentionally narrow: `.powerpacks/` artifacts only, backed by
local msgvault metadata. Do not use Powerset-hosted Gmail OAuth/sync helpers,
task JSON, or `../aleph-mvp`; code must live inside Powerpacks.

## Consent / approval model

Current V1 has no spend/write approval gates because it only reads local
msgvault metadata and writes local artifacts. Stop for explicit approval before
any uploads, source seeding, production mutation, or paid enrichment.

Do not assume EnrichLayer/RapidAPI keys exist. Do not call Powerset Gmail OAuth
or backend gmail-sync endpoints for this flow.

## msgvault metadata import

msgvault by Wes McKinney stores Gmail metadata in a local SQLite database,
usually `~/.msgvault/msgvault.db`. Powerpacks reads only `sources`,
`participants`, `messages`, and `message_recipients` to derive email/name/count
metadata; it never reads message bodies, subjects, snippets, raw MIME, or
attachments.

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py msgvault \
  --db ~/.msgvault/msgvault.db \
  --account-email <gmail-account-email>
```

This writes legacy Gmail CSV artifacts plus canonical
`.powerpacks/network-import/gmail/<run-id>/people.csv`.

## Legacy one-person seed

The primitive still has `run` / `continue` / `approve` for deterministic local
fixtures. Do not use those commands for real Gmail sync; use `msgvault`.

## Multi-account model

msgvault stores Gmail accounts in `sources`. Pass `--account-email` to filter to
one source account, or omit it to import metadata across all msgvault sources.
Later orchestrators can merge by email, LinkedIn URL, or resolved person ID. Do
not ask OpenAI to infer that multiple emails are the same person in V1.

## Sub-agent orchestration

For future long approved steps, the main agent can delegate `continue` to a
sub-agent and ask it to return only status, step id, artifacts, return code, and
log tail. Keep the main chat clean; never paste full CSVs or secrets.

## Output summary

End with:

- run ledger path
- run artifact directory
- generated CSV paths
- confirmation that no external APIs, DVC, uploads, or prod writes ran
