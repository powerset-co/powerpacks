# powerset_doctor

Single command that runs every prereq check the `powerset-login` skill needs
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
```

## Checks (in order)

| id | Verifies |
| --- | --- |
| `python` | Python 3.9+ on PATH |
| `gcloud_installed` | `gcloud` CLI on PATH |
| `gcloud_account` | active `@powerset.co` gcloud account |
| `gcloud_adc` | application-default credentials are present (warn-level) |
| `auth0_login` | a valid Auth0 JWT is cached at `~/.powerpacks/credentials.json` |
| `auth0_role` | the JWT carries a `user` or `admin` role (warn if neither) |
| `user_secrets` | the active gcloud account can read its per-user secrets in GCP |
| `env_file` | `.env` has every key required by the chosen profile |

## Statuses

- `ok` — nothing to do
- `warn` — works but degraded (often present but missing optional stuff)
- `missing` — needed for the chosen profile, has a `fix_command`
- `fail` — something the doctor itself could not evaluate

The top-level `overall` is `ok | warn | needs_setup`. Exit code is `0` only
when `overall == ok`.

## next_actions

A flat list of `fix_command` values from every `missing`/`fail` check, in
order, so the agent can offer them sequentially. Each `fix_command` is either
a string (single command) or an object with platform-specific variants
(`macos_homebrew`, `linux`, etc.).
