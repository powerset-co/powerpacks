# powerset_doctor

Single command that runs every prereq check the `$powerset login` flow needs
and returns a structured JSON report. Stdlib-only.

The agent reads this report and decides what to ask the user next. Every
check has a stable `id` and a `fix_command` when applicable so the skill can
walk the user through gaps one at a time.

## Usage

```bash
python packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search

# Optional, only for SDK/client debugging:
python packs/powerset/primitives/doctor/doctor.py run --check-adc

# Optional, only for env refresh/provisioning debugging when .env is already filled:
python packs/powerset/primitives/doctor/doctor.py run --check-user-secrets
```

## Checks (in order)

| id | Verifies |
| --- | --- |
| `python` | Python 3.12 runtime for Powerpacks |
| `uv_installed` | `uv` on PATH for Python dependency setup and primitive execution |
| `gcloud_installed` | `gcloud` CLI on PATH |
| `gcloud_account` | active `@powerset.co` gcloud account |
| `gcloud_adc` | application-default credentials are present (opt-in via `--check-adc`; not needed for normal Powerpacks workflows) |
| `auth0_login` | a valid Auth0 JWT is cached at `~/.powerpacks/credentials.json` |
| `auth0_role` | the JWT carries a `user` or `admin` role (warn if neither) |
| `env_file` | `.env` has every key required by the chosen profile |
| `user_secrets` | the active gcloud account can read its per-user secrets in GCP; checked only when `env_file` is incomplete, or with `--check-user-secrets` |

## Statuses

- `ok` — nothing to do
- `warn` — works but degraded (present but missing optional stuff)
- `missing` — needed for the chosen profile, has a `fix_command`
- `fail` — something the doctor itself could not evaluate

The top-level `overall` is `ok | warn | needs_setup`. Exit code is `0` only
when `overall == ok`.

## next_actions

A flat list of `fix_command` values from every `missing`/`fail` check, in
order, so the agent can offer them sequentially. Each `fix_command` is either
a string (single command) or an object with platform-specific variants
(`macos_homebrew`, `linux`, etc.).
