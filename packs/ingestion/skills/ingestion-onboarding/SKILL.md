---
name: ingestion-onboarding
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Use the onboarding primitive:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py check
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py plan
```

It tracks non-secret state in `.powerpacks/ingestion/accounts.json`.

Never store tokens/passwords/cookies there. Only store usernames, linked status,
artifact paths, and notes.
