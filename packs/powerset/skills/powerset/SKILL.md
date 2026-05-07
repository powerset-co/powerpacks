---
name: powerset
description: Unified Powerset command surface. Use for `$powerset login`, `$powerset status`, `$powerset whoami`, `$powerset sets list`, `$powerset sets use <id|name>`, `$powerset mcp install`, `$powerset env pull`, and `$powerset help`.
---

# Powerset

Use this skill when the user asks for `$powerset ...` or wants Powerset setup,
status, identity, MCP registration, runtime env provisioning, or default set
selection.

## Command routing

| User command | Do this |
| --- | --- |
| `$powerset`, `$powerset help` | Print the supported subcommands below. |
| `$powerset login` | Run the login workflow below. |
| `$powerset status`, `$powerset doctor` | Run the doctor read-only and summarize blockers. |
| `$powerset whoami` | Run the Auth0 `whoami` primitive. |
| `$powerset sets`, `$powerset sets list` | List sets via the Powerset Search MCP. |
| `$powerset sets use <id|name>` | Resolve a set via MCP and write `POWERPACKS_DEFAULT_SET_ID` to local `.env`. |
| `$powerset mcp install` | Register/refresh the `powerset-search` MCP for local hosts. |
| `$powerset env pull [--profile <profile>]` | Pull allowlisted runtime keys into `.env` after confirming intent. |

Aliases remain valid for backcompat: `$powerset-login` means `$powerset login`;
`$powerset-set` means `$powerset sets` / `$powerset sets use`.

## Help text

When asked for help, respond with:

```text
$powerset login                 log in and provision local Powerpacks config
$powerset status|doctor         check local setup
$powerset whoami                show current Powerset/Auth0 identity
$powerset sets list             list visible Powerset sets
$powerset sets use <id|name>    set local default set in .env
$powerset mcp install           install/refresh powerset-search MCP
$powerset env pull              pull profile keys into .env
$powerset help                  show this help
```

## Path setup

Resolve primitive paths from the current environment:

- From the Powerpacks repo root, use `packs/powerset/primitives/...`.
- From an installed skill bundle, use `powerpacks/packs/powerset/primitives/...`.
- If a path does not exist, locate it with `rg --files -g 'doctor.py' -g
  'auth.py' -g 'provision_runtime_env.py' -g 'mcp_install.py'`.

Prefer `python3` if `python` is not on PATH.

## `$powerset login`

Run one read-only check:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

The doctor intentionally does **not** check gcloud application-default
credentials. ADC is not needed for normal Powerpacks workflows.

If `overall == "ok"`, tell the user they are already set up and stop. Otherwise
handle the doctor's `fix_kind` values:

- `auto`: run the primitive directly from this shell.
- `interactive`: run the CLI directly from this shell. For browser/code flows,
  keep prompts visible to the user.
- `shell_install`: ask before running any OS-level install command.
- `human_action`: tell the user what maintainer/Slack action is required.

Common direct fixes:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/auth/auth.py login
uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core --env-file .env --confirm --best-effort
uv run --project powerpacks python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

Do **not** run `doctor.py fix` in the normal login flow; it can hide
interactive work inside a nested subprocess. Never print secret values.

The doctor only probes per-user Secret Manager access when `.env` is incomplete
(or when `--check-user-secrets` is used for refresh debugging). If `.env` already
has the requested profile keys, expired gcloud Secret Manager auth is not a
login blocker and should not be surfaced.

If the doctor reports `user_secrets` with `fix_kind: interactive` or a message
like `gcloud credentials need reauthentication`, this is not a Slack/IAM issue:
the selected `@powerset.co` account is fine, but gcloud's cached token expired.
When the user asks to proceed, run `gcloud auth login --no-launch-browser`,
relay the verification URL/code prompt, and ask them to paste the code.

Re-run doctor at the end and give a one-line summary. If `user_secrets` is still
a human-action blocker, tell the user to ping `#powerpacks` with their
`@powerset.co` email so a maintainer can provision per-user secrets.

## `$powerset status` / `$powerset doctor`

Run doctor read-only with the requested or default profile:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

Summarize `overall`, missing/fail checks, and `next_actions`. Do not mention ADC
unless the user explicitly asks for application-default credential debugging.

## `$powerset whoami`

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/auth/auth.py whoami
```

Report the email and authorization/role. Do not print raw tokens.

## `$powerset mcp install`

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

If the user asks only to inspect MCP state, run:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py status --host all
```

## `$powerset env pull`

Default profile is `search-core` unless the user specifies another profile.
Confirm before writing `.env`, then run:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm \
  --best-effort
```

## `$powerset sets list` / `$powerset sets use <id|name>`

Use the Powerset Search MCP for set retrieval and name resolution. Do not use
local SQL for normal set listing.

For `sets list`, call the MCP `list_sets` tool and show concise rows with name,
set ID, role, and counts.

For `sets use <id|name>`:

1. Call MCP `list_sets`.
2. Resolve the user's value against set IDs and names.
3. Upsert exactly one local `.env` line:

```dotenv
POWERPACKS_DEFAULT_SET_ID=<set_id>
```

If `POWERPACKS_DEFAULT_SET_ID=` already exists, replace that line; otherwise
append it. Do not append duplicates. If only legacy `POWERSET_DEFAULT_SET_ID`
exists, leave it alone and add the preferred `POWERPACKS_DEFAULT_SET_ID` line.

After setting, report the selected set name, `set_id`, `person_count`,
`member_count`, and `sales_nav_account_count` when available.
