# powerset_auth

Auth0 PKCE login for the Powerset / search-api. Stdlib-only.

Stores credentials at `~/.powerpacks/credentials.json` (parent dir 0700,
file 0600). Deliberately separate from `contact-exporter`'s
`~/.powerset/credentials.json` so the two tools don't fight over token state.

## Usage

```bash
# Interactive login: opens browser, captures Auth0 callback on 127.0.0.1:9876.
python packs/powerset/primitives/auth/auth.py login

# Show stored credentials (no refresh).
python packs/powerset/primitives/auth/auth.py whoami

# Get a fresh access token (auto-refreshes if expiring within 60s).
python packs/powerset/primitives/auth/auth.py token

# Plain bearer token on stdout, useful for shell pipelines:
TOKEN=$(python packs/powerset/primitives/auth/auth.py token --bearer-only)
curl -H "Authorization: Bearer $TOKEN" https://...

# Wipe credentials.
python packs/powerset/primitives/auth/auth.py logout
```

## Environment overrides

| Variable | Default |
| --- | --- |
| `POWERPACKS_AUTH0_DOMAIN` | required for `login` / token refresh |
| `POWERPACKS_AUTH0_CLIENT_ID` | required for `login` / token refresh |
| `POWERPACKS_AUTH0_AUDIENCE` | required for `login` |
| `POWERPACKS_AUTH0_SCOPES` | `openid profile email offline_access` |
| `POWERPACKS_AUTH_CALLBACK_PORT` | `9876` |
| `POWERPACKS_CREDENTIALS_PATH` | `~/.powerpacks/credentials.json` |


For Powerset-hosted use, copy `packs/powerset/templates/env.powerset.example` to `.env` or export the listed variables explicitly.
