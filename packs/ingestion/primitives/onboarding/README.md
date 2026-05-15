# onboarding

Guided local onboarding for network ingestion sources.

It reads/writes `.powerpacks/ingestion/accounts.json`, checks local artifacts
where possible, and gives next actions for each channel.

Non-blocking conversational flow:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py run
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --input yes
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action yes
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py skip
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py run-status
```

Harnesses should prefer `continue --action ...` or `continue --csv ...` over
free-form `--input` replies. LinkedIn CSV actions:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action scan-linkedin-downloads
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action open-downloads
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action open-linkedin-drop-folder
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --action check-linkedin-drop-folder
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py continue --csv <Connections.csv>
```

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
- linkedin_mcp
- twitter
