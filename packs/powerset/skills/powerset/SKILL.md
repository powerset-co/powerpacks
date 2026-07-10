---
name: powerset
description: Unified Powerset command surface. Use for `$powerset setup`, `$powerset login`, `$powerset status`, `$powerset whoami`, `$powerset sets list`, `$powerset sets use [id-or-name]`, `$powerset mcp install`, `$powerset env pull`, `$powerset create oauth app`, and `$powerset help`.
---

# Powerset

Use this skill when the user asks for `$powerset ...` or wants Powerset setup,
status, identity, MCP registration, runtime key pull, or default set selection.
`$powerset setup` is the preferred one-command first-run path: it does Auth0
login, pulls provisioned runtime keys from the Powerset API, and registers the
MCP so users do not need to run multiple smaller commands.

## Command routing

| User command | Do this |
| --- | --- |
| `$powerset`, `$powerset help` | Print the supported subcommands below. |
| `$powerset setup` | Run the setup workflow below: ensure login, pull runtime keys, and install/refresh MCP. The explicit command is consent to write `.env`. |
| `$powerset login` | Run the login workflow below. |
| `$powerset status` | Run the setup check quietly and summarize only blockers. |
| `$powerset whoami` | Run the Auth0 `whoami` primitive. |
| `$powerset sets`, `$powerset sets list` | List sets via the Powerset Search MCP. |
| `$powerset sets use <id|name>` | Resolve a set via MCP and write `POWERPACKS_DEFAULT_SET_ID` to local `.env`. |
| `$powerset mcp install` | Register/refresh the `powerset-search` MCP for local hosts. |
| `$powerset env pull` | Pull your Modal token + OpenAI key from the Powerset API (using your Auth0 login) into `.env`. The explicit command is consent to write `.env`. |
| `$powerset create oauth app` | Route to the msgvault setup primitive for Gmail OAuth Desktop app guidance. |

Aliases remain valid for backcompat: `$powerset-login` means `$powerset login`;
`$powerset-set` means `$powerset sets` / `$powerset sets use`. If the user asks
for Powerset setup, runtime setup, or API key setup without naming a
subcommand, prefer `$powerset setup` over separate login/env commands. Keep
plain `$setup` routed to the ingestion/product setup skill, not this command.

## Help text

When asked for help, respond with:

```text
$powerset setup                 log in, pull runtime keys, and install/refresh MCP
$powerset login                 refresh Auth0 credentials and MCP config
$powerset status                check local setup
$powerset whoami                show current Powerset/Auth0 identity
$powerset sets list             list visible Powerset sets
$powerset sets use <id|name>    set local default set in .env
$powerset mcp install           install/refresh powerset-search MCP
$powerset env pull              pull Modal/OpenAI keys into .env
$powerset create oauth app      guide Gmail OAuth app setup for msgvault
$powerset help                  show this help
```

## Canonical repo setup

For mutating commands (`$powerset setup`, `$powerset login`, `$powerset env
pull`, `$powerset sets use`, `$powerset mcp install`, and `$powerset create
oauth app`), first resolve and enter the canonical non-`.codex` Powerpacks repo.
This ensures `.env` and any local Powerpacks state are written under the
installed checkout such as `~/powerpacks`, not under an agent skill bundle like
`~/.codex/powerpacks`.

Prefer, in order:

1. `$POWERPACKS_REPO_ROOT` if it points to a Powerpacks repo;
2. current working directory if it is a Powerpacks repo and not under `.codex`;
3. `~/powerpacks`;
4. `~/workspace/powerpacks`.

```bash
resolve_powerpacks_root() {
  for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" != *"/.codex/"* ]] || continue
    if [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
repo="$(resolve_powerpacks_root)" || {
  echo "No canonical non-.codex Powerpacks repo found. Install/copy Powerpacks to ~/powerpacks first." >&2
  exit 1
}
cd "$repo"
```

For read-only commands (`$powerset status`, `$powerset whoami`, `$powerset sets
list`), still prefer the canonical repo when available. If no canonical repo is
available, stop instead of writing or syncing from `~/.codex/powerpacks`.

Run primitives from the canonical repo with `uv run --env-file .env --project . python
packs/...`. Prefer `python3` only when invoking a local helper outside `uv`.

## `$powerset setup`

This is the preferred one-command setup path. It combines the user-facing pieces
people otherwise had to run separately:

1. ensure Powerset/Auth0 login is present;
2. pull allowlisted runtime env keys into local `.env`;
3. install/refresh the `powerset-search` MCP for local hosts.

The explicit `$powerset setup` request is consent to write `.env`; do not ask
for a separate env-write confirmation. It is not the same as bare `$setup`,
which stays the ingestion/product setup flow. Modal handles hosted processing
for provisioned Powerset users.

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
uv run --env-file .env --project . python packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env
```

Handle `fix_kind` values exactly as in the `$powerset login` workflow below.
In particular, run direct primitives/CLIs from this shell rather than nested
doctor fix commands so browser/code prompts stay visible.

If `auth0_login` is missing or expired, run the Auth0 login directly:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/auth/auth.py login
```

After Auth0 login, always run the env pull so rotated or newly added keys land
in `.env`, even if the initial setup check was already healthy. This pulls your
Modal token + OpenAI key from the Powerset API using your Auth0 bearer:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull \
  --env-file .env
```

If it reports `not_provisioned`, an admin must provision your Modal token out of
band; relay that one-line action and continue.

Then install/refresh MCP:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

Re-run the setup check at the end and use the success/blocker message above. If
`runtime_keys` is still missing, the env pull reported `not_provisioned` — tell
the user an admin must provision their Modal token / OpenAI key for their
Powerset user (the endpoints never mint).

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
uv run --env-file .env --project . python packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env
```

If `overall == "ok"`, use the already-valid success sentence above and stop.
Otherwise handle `fix_kind` values internally:

- `auto`: run the primitive directly from this shell.
- `interactive`: run the CLI directly from this shell. For browser/code flows,
  keep prompts visible to the user.
- `shell_install`: ask before running any OS-level install command.
- `human_action`: tell the user what maintainer/Slack action is required.

Common direct fixes:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/auth/auth.py login
uv run --env-file .env --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull --env-file .env
uv run --env-file .env --project . python packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

Do not run nested fix commands in the normal login flow; they can hide
interactive work inside a subprocess. Run the direct primitives above instead.
Never print secret values.

Re-run the setup check at the end and use one of the terse final messages above.
If `runtime_keys` is still missing after the env pull, the API returned
`not_provisioned` — tell the user an admin must provision their Modal token /
OpenAI key for their Powerset user (the endpoints never mint).

## `$powerset status`

Run the setup check read-only with the requested or default profile:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env
```

Summarize only `Ready` or the concise blocker/action. Do not mention the checker,
raw counts or individual passed checks unless the user explicitly asks for
debugging detail.

## `$powerset whoami`

```bash
uv run --env-file .env --project . python packs/powerset/primitives/auth/auth.py whoami
```

Report the email and authorization/role. Do not print raw tokens.

## `$powerset mcp install`

```bash
uv run --env-file .env --project . python packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

If the user asks only to inspect MCP state, run:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/mcp_install/mcp_install.py status --host all
```

## `$powerset env pull`

Pulls the keys the local machine needs into `.env` from the Powerset API using
your Auth0 login. Heavy work runs on Modal (which holds
RapidAPI/Parallel/etc. as workspace secrets), so the laptop only needs a Modal
token (to dispatch) and an OpenAI key (local search LLM steps). The explicit
`$powerset env pull` request is consent to write `.env`; do not ask for separate
confirmation. Run:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull \
  --env-file .env
```

This fetches `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` and `OPENAI_API_KEY`. If the
pull reports `not_provisioned` for a key, an admin must provision it for your
Powerset user out of band (the endpoints never mint) — relay that one-line
action and continue. Requires a valid Auth0 login (`$powerset login`).

## `$powerset create oauth app`

This alias is for msgvault/Gmail OAuth setup. Prefer browser automation:

```bash
uv run --env-file .env --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup
```

If the user only wants instructions, run:

```bash
uv run --env-file .env --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py create-oauth-app
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
