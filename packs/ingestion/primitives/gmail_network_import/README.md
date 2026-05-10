# gmail_network_import

Resumable local Gmail network-import orchestrator.

V1 is **one person only** and **Powerpacks-local only**. It ports the legacy
Gmail contact CSV contracts and header parsing/normalization logic into
Powerpacks, with no runtime dependency on `../aleph-mvp`.

It writes `.powerpacks/` artifacts and exits complete. No Gmail API, DVC, paid
APIs, uploads, Harmonic enrichment, or production source seeding run in V1.

## Main loop

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py run \
  --email jane@example.com \
  --name "Jane Example" \
  --account-email me@gmail.com \
  --account-id gmail-account-1

uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py status
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py continue
```

The command contract is still `run` / `continue` / `approve` so future paid or
OAuth-backed stages can gate cleanly, but current V1 has no approval gates.

## Server-linked Gmail accounts

Use the local Powerset token to list Gmail accounts already connected in the
Powerset app:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py accounts
```

Open the existing browser OAuth flow:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py connect
```

`connect` opens `https://search.powerset.dev/gmail`. It does not put the local
Powerpacks bearer token in the URL. The browser app handles Auth0, starts Google
OAuth, and stores Google tokens server-side in encrypted Supabase tables.

## Artifacts

Run artifacts live under `.powerpacks/network-import/gmail/<run-id>/`:

- `accounts.csv` — local account registry row, supports one run per Gmail account
- `source_contact.jsonl`
- `gmail_threads_<account>_<op>.csv`
- `gmail_contacts_aggregated_<account>_<op>.csv`
- `targeted_emails_<account>_<op>.csv`
- `domain_context.json` — local domain/company heuristic, not OpenAI
- `manifest.json`
- `workspace.json`
- `next-steps.json`

## Notes

- Multiple Gmail accounts are modeled as separate local runs with different
  `--account-email` / `--account-id`; merging is a future local stage.
- OpenAI domain parse is not needed for this one-person V1; the local heuristic
  derives `example.com -> Example`.
- EnrichLayer/RapidAPI/Parallel/Harmonic are intentionally not assumed present.
- Future Gmail sync should be implemented inside this pack, likely via Powerset
  OAuth + scoped metadata exports; agents should not rely on raw refresh tokens
  being available locally.
