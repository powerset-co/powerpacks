---
name: ingestion-onboarding
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Use the onboarding primitive for status/plan checks:

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

For guided CLI operation, prefer the idempotent `step` loop. The harness should
keep calling the same command until it returns `completed`, `needs_input`,
`waiting`, or `blocked_approval`; then echo the emitted prompt/commands, ask the
operator to add/confirm/skip, and rerun `step`.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step
```

Gmail is msgvault-backed. The Gmail step should be dead simple for the user:
ask which discovered accounts to import and what other Gmail addresses they want
to add. Multiple discovered source accounts are supported by repeating
`--gmail-account`; `--gmail-all` imports every discovered source account;
`--gmail-add-email` starts the add-account flow for new addresses;
`--skip-source gmail` records an explicit skip.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-db ~/.msgvault/msgvault.db \
  --gmail-account me@gmail.com \
  --gmail-account work@example.com
```

If `step` returns `status: needs_agent_action` with `commands`, Codex must run
the returned commands in order. Do not tell the user to run them. For extra
Gmail addresses this means Codex runs the Google OAuth test-user browser
automation, authorizes each Gmail account in msgvault, starts per-account
msgvault sync in the background, and reruns onboarding after a local checkpoint
exists. Large mailboxes can take a few hours to fully sync; tell the user the
current synced message count and log path instead of blocking the main thread.
Only ask the user to complete browser login/consent when Google requires human
action.

LinkedIn CSV remains the primary LinkedIn path:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user <label>
```

If `blocked_approval` is returned for LinkedIn provider enrichment, ask the
operator before running the emitted `approval_command`, then rerun `step` until
`.powerpacks/ingestion/accounts.json` shows `linkedin_csv.linked: true`.
Other missing sources can be marked done with `--skip-source <messages|gmail|linkedin_csv|twitter>`.

It tracks non-secret state in `.powerpacks/ingestion/accounts.json`.

Never store tokens/passwords/cookies there. Only store usernames, linked status,
artifact paths, and notes.
