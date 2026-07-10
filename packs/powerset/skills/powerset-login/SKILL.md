---
name: powerset-login
description: One-command Powerset login flow. Quietly refreshes Auth0 credentials, pulls provisioned runtime keys from the Powerset API, refreshes MCP registration, and only stops for OS installs, visible auth codes, or human actions.
---

# Powerset Login

Use this skill when the user asks for `$powerset login`, `$powerset-login`,
or "log me in to Powerset". For first-run Powerset setup, runtime setup, or
API key setup, prefer the unified `$powerset setup` command in
`packs/powerset/skills/powerset/SKILL.md`; it intentionally runs login plus
runtime key pull plus MCP registration. This alias remains the right skill when
an unrelated Powerpacks command failed because of a missing key or expired
session.

**This skill is built to be fast and quiet.** The user said "log me in" - do
that. Don't ask permission for every step. Use setup-check classifications to
decide what needs asking, but run fixes yourself from the current
invocation/shell so prompts, URLs, and failures are visible.

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

## Core Rule

Use the read-only setup checker for diagnosis. In the normal skill flow, do not
run nested fix commands; they can hide interactive work inside a subprocess and
swallow browser/code prompts. Let the skill self-heal by running the relevant
primitive or CLI command directly.

Only use install commands when something is actually missing. Do not reinstall
Powerpacks adapters, MCP config, or credentials when the setup check says the
check is already `ok`.

## Canonical Repo Setup

Resolve and enter the canonical non-`.codex` Powerpacks repo before running any
Powerset login/setup command. This ensures `.env` and local Powerpacks state are
written under the installed checkout such as `~/powerpacks`, not under an agent
skill bundle like `~/.codex/powerpacks`.

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

Run primitives from the canonical repo with `uv run --env-file .env --project . python packs/...`.

## Happy Path

Run one internal read-only setup check:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env
```

If `overall == "ok"`, use the already-valid success sentence above and stop.

## How To Handle The Setup Report

The setup check returns one JSON object. The fields you care about are `checks`,
`by_fix_kind`, and `next_actions`. Each missing/fail check has a `fix_kind`:

| `fix_kind` | What to do | Ask the user? |
| --- | --- | --- |
| `auto` | Run the specific primitive directly from this shell. | no |
| `interactive` | Run the CLI directly from this shell. For browser/code flows, keep prompts visible. | no, except asking for an auth code if the CLI requires one |
| `shell_install` | OS-level install (`brew install`, etc.). | yes, with the exact command shown |
| `human_action` | Cannot be fixed by the skill. | tell the user, don't loop |

## Workflow

### Step 1 - Install only when missing

If the Powerpacks skill bundle or host adapter is missing and you are in the
Powerpacks repo, run the repo installer for the current host instead of trying
to repair it through a nested fix command:

```bash
./install.sh codex
./install.sh claude-code
```

Pick the host the user is currently using. If both hosts need repair, run both.

### Step 2 - Login directly from this shell

If `auth0_login` is missing or expired, run:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/auth/auth.py login
```

### Step 3 - Pull runtime keys and register MCP directly

If `runtime_keys` is missing, run:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull \
  --env-file .env
```

This pulls `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, and `OPENAI_API_KEY` from the
authenticated Powerset API when the user has been provisioned. Modal handles
hosted processing for Powerset users. The Google Cloud CLI remains relevant
only to the separate msgvault/Gmail OAuth app setup flow.

If `mcp_powerset_search` is missing but a host CLI exists, run:

```bash
uv run --env-file .env --project . python packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

If `mcp_powerset_search` says no MCP host CLI is on PATH, that is an install
gap: use `./install.sh codex` or `./install.sh claude-code` only for the host
the user is using, or tell the user which host CLI is missing.

### Step 4 - Human-action blockers

`human_action` entries:

- `auth0_role` warn: "You're logged in but haven't been granted a Powerpacks
  role. Ask a Powerset admin to add your account to the Powerpacks Auth0 role."
- `runtime_keys` `not_provisioned`: "You're logged in, but your Modal/OpenAI
  runtime keys have not been provisioned for the Powerset API yet. Ask a
  Powerset admin to provision them, then rerun `$powerset login`."

These are not blockers for unrelated workflows unless that workflow needs the
missing runtime key.

### Step 5 - Final state

Re-run the setup check and report a terse final message:

- `overall: ok` -> "Credentials updated. Please restart Codex to reload the
  Powerset MCP token."
- `overall: warn` -> same, but mention the remaining warning in one short phrase.
- `overall: needs_setup` -> list the still-blocked items and what the user
  needs to do.

## Hard Rules

- Never print secret values. Every primitive redacts.
- Never run an OS install command without explicit user approval.
- Never write `.env` unless the user explicitly requested `$powerset setup`,
  `$powerset login`, or `$powerset env pull`.
- Runtime-key authorization is enforced by the Powerset API using the cached
  Auth0 bearer token; do not add a separate email-domain gate.
