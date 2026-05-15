---
name: onboard
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Use the onboarding primitive:

Start/resume the conversational setup flow:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py run
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --input <user-reply>
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py skip
```

When a primitive response has `status: needs_agent_action`, Codex must run the
returned `command` itself. Do not tell the user to run it. After the command
finishes, run the returned `continue_command` when present, or continue with
`done`.

When a primitive response has `status: needs_user_action`, perform any returned
local `command` that Codex can run, then tell the user only the human action
that remains, such as browser OAuth or QR/device linking.

When a primitive response has `status: needs_user_input`, ask the question
directly and continue with the user's reply.

Check/plan without entering the flow:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py check
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py plan
```

It tracks non-secret state in `.powerpacks/ingestion/accounts.json`.

Never store tokens/passwords/cookies there. Only store usernames, linked status,
artifact paths, and notes.
