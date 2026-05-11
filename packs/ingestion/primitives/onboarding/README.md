# onboarding

Guided local onboarding for network ingestion sources.

It reads/writes `.powerpacks/ingestion/accounts.json`, checks local artifacts
where possible, and gives next actions for each channel.

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
