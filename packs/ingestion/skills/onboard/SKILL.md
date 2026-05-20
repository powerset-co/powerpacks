---
name: onboard
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Onboarding is the top-level Codex-run state machine. Task 2 onboarding is
**link-only**: it records source links in `.powerpacks/ingestion/accounts.json`
and must not run `gmail_network_import`, `linkedin_network_import`,
`import_network_pipeline`, Twitter crawls, messages import/research, or
downstream enrichment. Keep human/browser account linking in the main thread,
then hand long local import/index work to worker sub-agents only after the
completed handoff and user confirmation.

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
on a first run, ask which Gmail address they want to link first. Do not infer
the email from gcloud, Powerset login, git config, local status, or old
artifacts. After the user answers, run `step --gmail-add-email <email>`.

Once msgvault has a local checkpoint, ask which discovered accounts to link
and what other Gmail addresses they want to add. Multiple discovered source
accounts are supported by repeating `--gmail-account`; `--gmail-all` records
every discovered source account; `--gmail-add-email` starts the add-account
flow for new addresses; `--skip-source gmail` records an explicit skip.

Fresh Gmail start:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-add-email me@gmail.com
```

Record discovered Gmail accounts:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-db ~/.msgvault/msgvault.db \
  --gmail-account me@gmail.com \
  --gmail-account work@example.com
```

If `step` returns `status: needs_agent_action` with `commands`, Codex must run
the returned commands in order. Do not tell the user to run them. For extra
Gmail addresses this means Codex runs the Google OAuth test-user browser
automation and authorizes each Gmail account in msgvault as user-action/linking.
These commands may include `msgvault_setup.py add-test-users`, `add-account`, or
`browser-setup --add-account`; they are not network imports. Rerun onboarding
after msgvault has source accounts to select.
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

The LinkedIn step only validates and records the CSV path/source label; it does
not run enrichment or imports. Messages and Twitter are also link-only:
`--messages-contacts-csv <path>` records a contacts CSV and `--twitter-handle
<handle>` records a handle. Other missing sources can be marked done with
`--skip-source <messages|gmail|linkedin_csv|twitter>`.

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

The handoff import command is the first place that calls
`import_network_pipeline.py run --from-accounts ...`; onboarding itself never
does. Never store tokens/passwords/cookies there. The v2 registry stores
non-secret config (`gmail.msgvault_db/account_emails/oauth_app/oauth_test_users/available_accounts/selected_accounts`,
`linkedin_csv.csv_path/source_label`, `twitter.handle`,
`messages.contacts_csv`) while preserving v1 `usernames`/`artifacts` mirrors.
