# onboarding

Guided local onboarding for network ingestion sources.

It reads/writes `.powerpacks/ingestion/accounts.json`, checks local artifacts
where possible, and gives next actions for each channel.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py status
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py check
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py plan
```

For a repeatable CLI/harness loop, call `step` over and over. It refreshes
`.powerpacks/ingestion/accounts.json` from local artifacts, then returns the
next input/action/approval gate as JSON. The harness should echo the emitted
commands and ask the operator to choose add/confirm/skip when needed.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step
```

Gmail is msgvault-backed. When `step` finds `~/.msgvault/msgvault.db`, it lists
source Gmail accounts and asks the operator to add one or more accounts, import
all, or skip Gmail:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-db ~/.msgvault/msgvault.db \
  --gmail-account me@gmail.com \
  --gmail-account work@example.com

uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-db ~/.msgvault/msgvault.db \
  --gmail-all

uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --skip-source gmail
```

LinkedIn CSV remains the primary LinkedIn path:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user arthur
```

If LinkedIn enrichment is blocked for paid external APIs, run the emitted
`approval_command` only after operator approval, then keep rerunning `step`.
Other missing sources can be marked done with
`--skip-source <messages|gmail|linkedin_csv|twitter>`.

Channels:

- messages
- gmail
- linkedin_csv
- twitter
