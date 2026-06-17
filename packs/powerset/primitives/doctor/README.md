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
  --env-file .env
```

`search-core` is the default profile. The doctor checks Auth0, local runtime
keys pulled from the Powerset API, and MCP registration. Modal handles hosted
processing for provisioned Powerset users.

## Checks (in order)

| id | Verifies |
| --- | --- |
| `python` | Python 3.12 runtime for Powerpacks |
| `uv_installed` | `uv` on PATH for Python dependency setup and primitive execution |
| `auth0_login` | a valid Auth0 JWT is cached at `~/.powerpacks/credentials.json` |
| `auth0_role` | the JWT carries a `user` or `admin` role (warn if neither) |
| `runtime_keys` | `.env` has the Modal token and OpenAI key needed locally |
| `mcp_powerset_search` | the `powerset-search` MCP is registered in a local host |

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

Browser-based commands such as Auth0 login may include `fix_kind: interactive`.
In that case, run the command directly in a visible terminal/PTY so the browser
or code prompt remains visible.
