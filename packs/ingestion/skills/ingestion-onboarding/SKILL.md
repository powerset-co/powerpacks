---
name: ingestion-onboarding
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

Harnesses should prefer structured `continue --action ...` / `--csv ...` flags
over free-form replies whenever possible:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action yes
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action no
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action skip
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action done
```

For the LinkedIn CSV handoff, use the `harness_actions` commands returned by the
primitive, or these equivalent flags:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action scan-linkedin-downloads
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action open-downloads
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action open-linkedin-drop-folder
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action check-linkedin-drop-folder
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --csv <Connections.csv>
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
