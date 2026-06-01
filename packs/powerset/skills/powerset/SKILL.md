---
name: powerset
description: Unified Powerset command surface. Use for `$powerset setup`, `$powerset login`, `$powerset status`, `$powerset whoami`, `$powerset sets list`, `$powerset sets use <id|name>`, `$powerset mcp install`, `$powerset env pull`, `$powerset create oauth app`, and `$powerset help`.
---

# Powerset

Use this skill when the user asks for `$powerset ...` or wants Powerset setup,
status, identity, MCP registration, runtime env provisioning, operator bootstrap
sync, or default set selection. `$powerset setup` is the preferred one-command
first-run path: it
does login, runtime env pull, published operator bootstrap sync, and MCP
registration so users do not need to run multiple smaller commands.

## Command routing

| User command | Do this |
| --- | --- |
| `$powerset`, `$powerset help` | Print the supported subcommands below. |
| `$powerset setup [--profile <profile>]` | Run the setup workflow below: ensure login, pull env, sync any matching operator bootstrap, and install/refresh MCP. The explicit command is consent to write `.env`. |
| `$powerset login` | Run the login workflow below. |
| `$powerset status` | Run the setup check quietly and summarize only blockers. |
| `$powerset whoami` | Run the Auth0 `whoami` primitive. |
| `$powerset sets`, `$powerset sets list` | List sets via the Powerset Search MCP. |
| `$powerset sets use <id|name>` | Resolve a set via MCP and write `POWERPACKS_DEFAULT_SET_ID` to local `.env`. |
| `$powerset mcp install` | Register/refresh the `powerset-search` MCP for local hosts. |
| `$powerset env pull [--profile <profile>]` | Pull allowlisted runtime keys into `.env`. The explicit command is consent to write `.env`. Default `search-core` is the standard setup; use `search-network` or `import-contacts` for minimal per-user provisioning. |
| `$powerset create oauth app` | Route to the msgvault setup primitive for Gmail OAuth Desktop app guidance. |

Aliases remain valid for backcompat: `$powerset-login` means `$powerset login`;
`$powerset-set` means `$powerset sets` / `$powerset sets use`. If the user asks
for Powerset setup, runtime setup, or API key bootstrap without naming a
subcommand, prefer `$powerset setup` over separate login/env commands. Keep
plain `$setup` routed to the ingestion/product setup skill, not this command.

## Help text

When asked for help, respond with:

```text
$powerset setup                 log in, pull env, sync bootstrap, and install/refresh MCP
$powerset login                 refresh Auth0 credentials and MCP config
$powerset status                check local setup
$powerset whoami                show current Powerset/Auth0 identity
$powerset sets list             list visible Powerset sets
$powerset sets use <id|name>    set local default set in .env
$powerset mcp install           install/refresh powerset-search MCP
$powerset env pull              pull profile keys into .env
$powerset create oauth app      guide Gmail OAuth app setup for msgvault
$powerset help                  show this help
```

## Path setup

Resolve primitive paths from the current environment:

- From the Powerpacks repo root, use `packs/powerset/primitives/...`.
- From an installed skill bundle, use `powerpacks/packs/powerset/primitives/...`.
- If a path does not exist, locate it with `rg --files -g 'doctor.py' -g
  'auth.py' -g 'provision_runtime_env.py' -g 'operator_bootstrap.py' -g
  'mcp_install.py'`.

Prefer `python3` if `python` is not on PATH.

## `$powerset setup`

This is the preferred one-command setup path. It combines the user-facing pieces
people otherwise had to run separately:

1. ensure Powerset/Auth0 login is present;
2. pull allowlisted runtime env keys into local `.env`;
3. sync any published operator bootstrap bundle into local `.powerpacks/`;
4. install/refresh the `powerset-search` MCP for local hosts.

Default profile is `search-core` unless the user specifies `--profile <name>`.
The explicit `$powerset setup` request is consent to write `.env`; do not ask
for a separate env-write confirmation. It is not the same as bare `$setup`,
which stays the ingestion/product setup flow.

User-facing output must be terse:

- Start with exactly: `Setting up Powerset...`
- Do not narrate setup checks, missing check names, token formats, MCP config
  details, or successful substeps.
- If a browser/code login is needed, show only the auth URL/code prompt.
- On success, say exactly:
  `Powerset setup complete. Please restart Codex to reload the Powerset MCP token.`
- If still blocked, give one short sentence with the required action. Do not
  paste raw reports or secret values.

Run one internal setup check first:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

Handle `fix_kind` values exactly as in the `$powerset login` workflow below.
In particular, run direct primitives/CLIs from this shell rather than nested
doctor fix commands so browser/code prompts stay visible.

If `auth0_login` is missing or expired, run the Auth0 login directly:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/auth/auth.py login
```

After Auth0 credentials and gcloud access are usable, always run the env pull
for the requested/default profile, even if the initial setup check was already
healthy. This makes `$powerset setup` the single refresh command for rotated or
newly added per-user runtime keys:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm \
  --best-effort
```

If this reports expired gcloud credentials, immediately run:

```bash
gcloud auth login --no-launch-browser
```

Relay the URL/code prompt tersely, then rerun the env pull command.

Then sync the operator bootstrap:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/operator_bootstrap/operator_bootstrap.py sync \
  --env-file .env
```

If this reports expired gcloud credentials, immediately run:

```bash
gcloud auth login --no-launch-browser
```

Relay the URL/code prompt tersely, then rerun the env pull command and the
operator bootstrap sync command. If bootstrap sync reports `skipped` because no
matching bundle is published or the registry is not available, continue with MCP
installation; `$setup` can still proceed from local account linking/import.

Then install/refresh MCP:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

Re-run the setup check at the end and use the success/blocker message above. If
`user_secrets` is still a human-action blocker, tell the user to ping
`#powerpacks` with the email they use for gcloud so a maintainer can provision
per-user secrets.

## `$powerset login`

User-facing output must be terse:

- Start with exactly: `Updating your credentials...`
- Do not narrate setup checks, missing check names, token formats, MCP config
  details, or successful substeps.
- If a browser/code login is needed, show only the auth URL/code prompt.
- On success, say exactly:
  `Credentials updated. Please restart Codex to reload the Powerset MCP token.`
- If everything was already valid, say:
  `Credentials are already up to date. Restart Codex if the Powerset MCP still fails.`
- For unresolved human-action blockers, give one short sentence with the action
  required. Do not paste raw reports.

Run one internal setup check:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

This check intentionally does **not** check gcloud application-default
credentials. ADC is not needed for normal Powerpacks workflows.

If `overall == "ok"`, use the already-valid success sentence above and stop.
Otherwise handle `fix_kind` values internally:

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

Do not run nested fix commands in the normal login flow; they can hide
interactive work inside a subprocess. Run the direct primitives above instead.
Never print secret values.

The setup check only probes per-user Secret Manager access when `.env` is incomplete
(or when `--check-user-secrets` is used for refresh debugging). If `.env` already
has the requested profile keys, expired gcloud Secret Manager auth is not a
login blocker and should not be surfaced.

If the setup check reports `user_secrets` with `fix_kind: interactive` or a message
like `gcloud credentials need reauthentication`, this is not a Slack/IAM issue:
the selected `@powerset.co` account is fine, but gcloud's cached token expired.
For explicit `$powerset setup`, `$powerset login`, or `$powerset env pull`
requests, immediately run `gcloud auth login --no-launch-browser`, relay the
verification URL/code prompt, and ask them to paste the code. Do not stop for a
separate yes/no confirmation.

Re-run the setup check at the end and use one of the terse final messages above.
If `user_secrets` is still
a human-action blocker, tell the user to ping `#powerpacks` with their
`@powerset.co` email so a maintainer can provision per-user secrets.

## `$powerset status`

Run the setup check read-only with the requested or default profile:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

Summarize only `Ready` or the concise blocker/action. Do not mention the checker,
raw counts, ADC, or individual passed checks unless the user explicitly asks for
debugging detail.

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
It pulls the standard Powerpacks runtime secrets into `.env` on a best-effort
basis. The explicit `$powerset env pull` request is consent to write `.env`;
do not ask for separate confirmation. Run:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm \
  --best-effort
```

If this reports expired gcloud credentials or the setup check reports
`user_secrets` with `fix_kind: interactive`, immediately run:

```bash
gcloud auth login --no-launch-browser
```

Relay the URL/code prompt tersely, then rerun the env pull command.

For non-`@powerset.co` users, do not block on the email domain. Secret Manager
is the source of truth: if the matching per-user secrets are missing or IAM does
not grant access, the pull reports `not_provisioned` / `not_privileged`.
Useful minimal profiles:

- `search-network`: local search execution (`TURBOPUFFER_API_KEY`,
  `DATABASE_URL`, `OPENAI_API_KEY`)
- `import-contacts`: LLM review + Parallel research
  (`OPENROUTER_API_KEY`, `PARALLEL_API_KEY`)

## `$powerset create oauth app`

This alias is for msgvault/Gmail OAuth setup. Prefer browser automation:

```bash
uv run --project powerpacks python powerpacks/packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup
```

If the user only wants instructions, run:

```bash
uv run --project powerpacks python powerpacks/packs/ingestion/primitives/msgvault_setup/msgvault_setup.py create-oauth-app
```

If the user provided an email, add `--email <gmail>`. If they provided a Google
Cloud project, add `--project <project>`. Report the browser action and the
continuation command with `--client-secret`.

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
