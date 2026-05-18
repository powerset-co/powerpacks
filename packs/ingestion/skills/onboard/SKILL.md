---
name: onboard
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Use the onboarding primitive for status/plan checks:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py check
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py plan
```

For guided CLI operation, prefer the idempotent `step` loop. Keep calling the
same command until it returns `completed`, `needs_input`, `waiting`, or
`blocked_approval`; then echo the emitted prompt/commands, ask the operator to
add/confirm/skip, and rerun `step`.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step
```

Gmail is msgvault-backed. If `step` discovers local msgvault Gmail accounts, ask
which source accounts to add. Multiple Gmail source accounts are supported by
repeating `--gmail-account`; `--gmail-all` imports every discovered source
account; `--skip-source gmail` records an explicit skip.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-db ~/.msgvault/msgvault.db \
  --gmail-account me@gmail.com \
  --gmail-account work@example.com
```

LinkedIn CSV remains the primary LinkedIn path:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user <label>
```

If `blocked_approval` is returned for LinkedIn provider enrichment, ask before
running the emitted `approval_command`, then rerun `step` until
`.powerpacks/ingestion/accounts.json` shows `linkedin_csv.linked: true`.
Other missing sources can be marked done with `--skip-source <messages|gmail|linkedin_csv|twitter>`.

It tracks non-secret state in `.powerpacks/ingestion/accounts.json`.

Never store tokens/passwords/cookies there. Only store usernames, linked status,
artifact paths, and notes.
