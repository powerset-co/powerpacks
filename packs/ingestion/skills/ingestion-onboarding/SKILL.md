---
name: ingestion-onboarding
description: Walk a user through linking/exporting all local network ingestion sources and persist non-secret status in .powerpacks/ingestion/accounts.json.
---

# Ingestion Onboarding

Use the onboarding primitive for status/plan checks. Current onboarding is
**link-only**: record source links in `.powerpacks/ingestion/accounts.json` and
do not run Gmail/LinkedIn network imports, `msgvault sync-full`,
`import_network_pipeline`, Twitter crawls, messages import/research, or
enrichment until the completed handoff.

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
ask which discovered accounts to link and what other Gmail addresses they want
to add. Multiple discovered source accounts are supported by repeating
`--gmail-account`; `--gmail-all` records every discovered source account;
`--gmail-add-email` starts the OAuth/test-user/add-account authorization flow
for new addresses as pending accounts without starting msgvault sync or marking
them import-ready; `--skip-source gmail` records an explicit skip.

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
The returned commands route through `msgvault_setup.py add-test-users`,
`add-account`, or `browser-setup --add-account`; they are not network imports
and must not run `msgvault sync-full`. After those commands succeed, rerun the
emitted command with `--gmail-authorized-email <email>` so the account moves
from pending to linked; `$setup` import workers own `msgvault sync-full` before
reading the local msgvault DB.
Only ask the user to complete browser login/consent when Google requires human
action.

LinkedIn CSV remains the primary LinkedIn path:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user <label>
```

The LinkedIn step only records `--linkedin-csv` and `--linkedin-source-user`; it
does not run provider enrichment/import. Messages and Twitter are also
link-only: use `--messages-contacts-csv <path>` and optional `--twitter-handle
<handle>`, or skip sources with `--skip-source <messages|gmail|linkedin_csv|twitter>`.

It tracks non-secret v2 state in `.powerpacks/ingestion/accounts.json`:
`gmail.msgvault_db/account_emails/oauth_app/oauth_test_users/available_accounts/selected_accounts/pending_accounts`,
`linkedin_csv.csv_path/source_label`, `twitter.handle`, and
`messages.contacts_csv`, while preserving v1 `usernames`/`artifacts` mirrors.
When `step` returns completed, use the emitted handoff; its import phase calls
`import_network_pipeline.py run --from-accounts ...` after user confirmation.

Never store tokens/passwords/cookies there. Only store usernames, linked status,
artifact paths, and notes.
