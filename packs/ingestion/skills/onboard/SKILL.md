---
name: onboard
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Onboarding is the top-level Codex-run state machine. Keep human/browser account
linking in the main thread, then hand long local import/index work to worker
sub-agents after the user confirms.

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
action. If msgvault has no OAuth client configured yet, the first returned
command will be `browser-setup --email <gmail> --add-account`; run that before
test-user or sync commands.

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

## After Linking

When `step` returns `status: completed`, read the emitted `handoff` object.
Show the `confirmation_prompt` and ask once before long work. After approval:

- Spawn a `worker` sub-agent for `worker_phases[0]` (`import-network`). Tell it
  it is not alone in the repo, to run `dry_run_command` first, then
  `run_command`, and to return any approval gate to the main thread.
- After import-network completes, spawn a `worker` sub-agent for
  `worker_phases[1]` (`build-local-search-index`). Tell it to use the merged
  people CSV from the import phase, run `dry_run_command` first, then
  `run_command`, then `materialize_duckdb_command`.
- Keep the main thread terse: report approval gates, current counts, final
  artifact paths, and real failures. Close workers when they finish.

Do not put account-linking browser flows in a worker. The top-level Codex
orchestrator owns Gmail/msgvault linking, Google browser actions, LinkedIn CSV
handoff, message/WhatsApp linking, Twitter linking, and user confirmations.

Never store tokens/passwords/cookies there. Only store usernames, linked status,
artifact paths, and notes.
