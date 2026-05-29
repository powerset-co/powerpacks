---
name: onboard
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Onboarding is the top-level Codex-run state machine. Task 2 onboarding is
**link-only**: it records source links in `.powerpacks/ingestion/accounts.json`
and must not run `gmail_network_import`, `msgvault sync-full`,
`linkedin_network_import`, `import_network_pipeline`, Twitter crawls, messages
import/research, or downstream enrichment. Keep human/browser account linking in the main thread,
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

If msgvault already has local checkpoints, ask which discovered accounts to
link. Otherwise, `--gmail-add-email` records a pending authorization request and
returns authorization commands without marking the account import-ready or
starting sync. Multiple discovered source accounts are supported by repeating
`--gmail-account`; `--gmail-all` records every discovered source account;
`--gmail-add-email` starts the add-account flow for new addresses;
`--skip-source gmail` records an explicit skip.

Fresh Gmail start:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-add-email me@gmail.com
```

For first-run Gmail linking, the email supplied with `--gmail-add-email` is also
the Google account that should own/create the local OAuth project. The returned
setup command should pass an explicit deterministic `--project` derived from
that email and must require/login as the same email before project creation. Do
not pick a project from the active local `gcloud` state.

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
`browser-setup --add-account`; they are not network imports and must not start
`msgvault sync-full`. After those commands succeed, rerun the emitted command
with `--gmail-authorized-email <email>` so the account moves from pending to
linked; `$setup` import workers own msgvault sync for selected accounts.
Only ask the user to complete browser login/consent when Google requires human
action. If msgvault has no OAuth client configured yet, the first returned
command will be `browser-setup --email <gmail> --add-account`; run that before
test-user or additional add-account commands.

LinkedIn CSV remains the primary LinkedIn path:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user <label>
```

The LinkedIn step only validates and records the CSV path/source label; it does
not run enrichment or imports. Messages onboarding is also link-only: it runs
the scoped iMessage/Contacts permission check and checks WhatsApp auth/link
status. If WhatsApp is not linked, run the returned `import_whatsapp_wacli.py
auth` command or rerun with `--skip-messages-whatsapp`; neither path runs
WhatsApp sync or exports contacts. `--messages-contacts-csv <path>` remains a
legacy/manual override for recording an existing contacts CSV. Twitter records a
handle with `--twitter-handle <handle>`. Other missing sources can be marked
done with `--skip-source <messages|gmail|linkedin_csv|twitter>`.

It tracks non-secret state in `.powerpacks/ingestion/accounts.json`.

## After Linking

When `step` returns `status: completed`, read the emitted `handoff` object.
Show the `confirmation_prompt` and ask once before long work. After approval:

- Run `handoff.handoff_command`. That command is the only post-link handoff path
  and delegates import worker planning, approval confirmations, fan-in, and indexing
  readiness to `$setup` / `setup.py handoff`.
- Dispatch import/index workers only from the setup handoff response. Do not use
  legacy direct onboarding worker phases.
- Keep the main thread user-friendly: report connected sources, approval confirmations,
  current counts, final local paths, and real failures. Do not describe ledgers,
  fan-in/fan-out, or implementation details unless the user asks.

Do not put account-linking browser flows in a worker. The top-level Codex
orchestrator owns Gmail/msgvault linking, Google browser actions, LinkedIn CSV
handoff, message/WhatsApp linking, Twitter linking, and user confirmations.

The setup handoff is the first post-link place that plans imports. Onboarding
itself never runs imports. Never store tokens/passwords/cookies there. The v2
registry stores non-secret config (`gmail.msgvault_db/account_emails/oauth_app/oauth_test_users/available_accounts/selected_accounts`,
`linkedin_csv.csv_path/source_label`, `twitter.handle`,
`messages.imessage/whatsapp/planned_contacts_csv`) while preserving v1
`usernames`/`artifacts` mirrors.
