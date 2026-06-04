# onboarding

Guided local onboarding for network ingestion sources.

It reads/writes `.powerpacks/ingestion/accounts.json`, refreshes local artifact
state where possible, and gives the next action for each missing channel.

## Step Loop

Use the idempotent `step` command for harnesses and agents:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step
```

Rerun the same command until it returns one of:

- `completed`
- `needs_input`
- `waiting`
- `blocked_approval`

Gmail uses local msgvault metadata only. There is no hosted Powerset Gmail
connect flow in onboarding.

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --gmail-db ~/.msgvault/msgvault.db \
  --gmail-all
```

LinkedIn CSV can be provided when available:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user <label>
```

This records the export as linked input only. The later `discover-contacts`
handoff owns LinkedIn parsing, enrichment, and any spend approval gates.

Status/planning helpers:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py status
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py check
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py plan
```

Channels:

- messages
- gmail
- linkedin_csv
- twitter
