---
name: import-gmail-network
description: Incremental Gmail network import orchestrator. V1 runs one person locally in .powerpacks using run/continue/approve with no external repo dependency.
---

# Import Gmail Network

Use this skill when the user wants to import Gmail/network contacts into
Powerpacks.

V1 scope is intentionally narrow: `.powerpacks/` artifacts only. Prefer the
local msgvault metadata import when the user has synced Gmail with msgvault;
otherwise use the one-person seed or server-linked account helpers. Do not use
task JSON. Do not call `../aleph-mvp`; code must live inside Powerpacks.

## Consent / approval model

Current V1 has no spend/write approval gates because it only writes local
artifacts. Future stages must stop for:

- Gmail OAuth / account linking
- OpenAI / Parallel.ai / EnrichLayer / RapidAPI / Harmonic
- uploads, source seeding, or any production mutation

Do not assume EnrichLayer/RapidAPI keys exist. Prefer local/cache/Powerset
surfaces first.

## Server-linked Gmail accounts

Check connected Gmail accounts through the API using the local Powerset token:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py accounts
```

To connect another account, route the user to the existing Powerset web OAuth
flow:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py connect
```

Do not put local bearer tokens in browser URLs. The browser app handles Auth0;
Google tokens are stored server-side in encrypted Supabase `gmail_oauth_tokens`
and mapped via `user_gmail_mappings`.

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

## Main command loop

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py run \
  --email <contact-email> \
  --name "<contact-name>" \
  --account-email <gmail-account-email> \
  --account-id <stable-account-id-or-local>
```

Resume/status:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py status
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py continue
```

## Multi-account model

Run once per Gmail account. The run records `accounts.csv` plus account-scoped
CSV filenames. Later orchestrators can merge by email, LinkedIn URL, or resolved
person ID. Do not ask OpenAI to infer that multiple emails are the same person in
V1.

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
