---
name: msgvault
description: Set up msgvault for local Gmail archive access, including install/status, browser-assisted Google OAuth Desktop app creation, client_secret config, account auth, and Codex MCP registration. Use for `$msgvault`, `$local-msg-vault`, `msgvault setup`, `msgvault status`, `msgvault add-account`, `msgvault mcp install`, or `powerset create oauth app` when the target is Gmail/msgvault OAuth.
---

# msgvault

Use this skill when the user wants local Gmail archive setup through msgvault or
asks to create a Google OAuth app for msgvault.

## Commands

Run from the Powerpacks repo root:

```bash
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py status
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py browser-setup --email <gmail>
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py setup --email <gmail>
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py setup --client-secret <client_secret.json> --email <gmail>
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py create-oauth-app --email <gmail>
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py add-test-users <gmail>
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py add-account --email <gmail>
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py mcp-install
```

From an installed skill bundle, replace `packs/...` with
`powerpacks/packs/...`.

## Routing

- `$msgvault`, `$local-msg-vault`, status requests: run `status` and summarize readiness.
- `$msgvault setup`, `$local-msg-vault setup`, onboarding Gmail archive setup:
  run `browser-setup`; include `--email` if the user supplied one. Then run
  `add-account --email <gmail>` when the user is ready to authorize the Gmail
  account.
- `$msgvault create oauth app` or `$powerset create oauth app`: run
  `browser-setup` if the user wants automation, otherwise `create-oauth-app`;
  the Google OAuth Desktop client name should be `local-msg-vault`.
- User provides a downloaded `client_secret*.json`: run `setup --client-secret
  <path>` and add `--email` if known.
- Workspace org needs its own app: add `--oauth-app <short-name>`.
- Add OAuth test users: run `add-test-users <gmail>`; add `--project <id>`
  or `--oauth-app <short-name>` when targeting a non-default app.
- Headless machine: add `--headless` when authorizing the account.
- Reruns: `browser-setup` skips Google Console automation when a valid local
  client secret is already configured. Use `--force-browser-setup` only when
  intentionally creating/replacing the OAuth client.

## User-facing wording

Keep it direct:

- `msgvault is installed.`
- `Creating the Google OAuth app in Chrome.`
- `Downloaded client secret configured.`
- `OAuth app already configured.`
- `msgvault account authorized.`
- `msgvault MCP installed. Restart Codex to load it.`

Do not print client secret values or token paths beyond the configured JSON
file path. msgvault stores and searches Gmail message data locally; make that
clear before starting a sync, but do not over-explain when the user only asked
for setup.
