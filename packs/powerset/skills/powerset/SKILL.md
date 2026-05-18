---
name: powerset
description: Unified Powerset command surface. Use for `$powerset login`, `$powerset status`, `$powerset whoami`, `$powerset sets list`, `$powerset sets use <id|name>`, `$powerset mcp install`, `$powerset env pull`, `$powerset create oauth app`, and `$powerset help`.
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
| `$powerset status` | Run the setup check quietly and summarize only blockers. |
| `$powerset whoami` | Run the Auth0 `whoami` primitive. |
| `$powerset sets`, `$powerset sets list` | List sets via the Powerset Search MCP. |
| `$powerset sets use <id|name>` | Resolve a set via MCP and write `POWERPACKS_DEFAULT_SET_ID` to local `.env`. |
| `$powerset mcp install` | Register/refresh the `powerset-search` MCP for local hosts. |
| `$powerset env pull [--profile <profile>]` | Pull allowlisted runtime keys into `.env`. The explicit command is consent to write `.env`. Default `search-core` is the standard setup; use `search-network` or `import-contacts` for minimal per-user provisioning. |
| `$powerset create oauth app` | Route to the msgvault setup primitive for Gmail OAuth Desktop app guidance. |

Aliases remain valid for backcompat: `$powerset-login` means `$powerset login`;
`$powerset-set` means `$powerset sets` / `$powerset sets use`.

## Help text

When asked for help, respond with:

```text
$powerset login                 log in and provision local Powerpacks config
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
  'auth.py' -g 'provision_runtime_env.py' -g 'mcp_install.py'`.

Prefer `python3` if `python` is not on PATH.

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
For explicit `$powerset login` or `$powerset env pull` requests, immediately run
`gcloud auth login --no-launch-browser`, relay the verification URL/code prompt,
and ask them to paste the code. Do not stop for a separate yes/no confirmation.

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
