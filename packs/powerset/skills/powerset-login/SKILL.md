---
name: powerset-login
description: One-command Powerset login flow. Quietly refreshes credentials, env, and MCP registration, and only stops for OS installs, visible auth codes, or human actions.
---

# Powerset Login

Use this skill when the user asks for `$powerset login`, `$powerset-login`,
or "log me in to Powerset". For first-run Powerset setup, runtime setup,
secret provisioning, or API key bootstrap, prefer the unified `$powerset setup`
command in `packs/powerset/skills/powerset/SKILL.md`; it intentionally runs
login plus env pull plus MCP registration so users do not need multiple small
commands. This alias remains the right skill when an unrelated Powerpacks
command failed because of a missing key or expired session.

**This skill is built to be fast and quiet.** The user said "log me in" — do
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

## Core rule

Use the read-only setup checker for diagnosis. In the normal skill flow, do not
run nested fix commands; they can hide interactive work inside a subprocess and
swallow browser/code prompts. Let the skill self-heal by running the relevant
primitive or CLI command directly.

Only use install commands when something is actually missing. Do not reinstall
Powerpacks adapters, MCP config, `gcloud`, or credentials when the setup check says
the check is already `ok`.

## Path setup

Resolve primitive paths from the current environment:

- From the Powerpacks repo root, use `packs/powerset/primitives/...`.
- From an installed skill bundle, use `powerpacks/packs/powerset/primitives/...`.
- If the example command path does not exist, locate it with `rg --files -g
  'doctor.py' -g 'auth.py' -g 'provision_runtime_env.py' -g 'mcp_install.py'`.

Prefer `python3` if `python` is not on PATH.

## Happy path

Run one internal read-only setup check:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

If `overall == "ok"`, use the already-valid success sentence above and stop.
The setup check does not check gcloud application-default credentials by default;
ADC is not needed for normal Powerpacks workflows.

## How to handle the setup report

The setup check returns one JSON object. The fields you care about are `checks`,
`by_fix_kind`, and `next_actions`. Each missing/fail check has a `fix_kind`:

| `fix_kind` | What to do | Ask the user? |
| --- | --- | --- |
| `auto` | Run the specific primitive directly from this shell. | **no** |
| `interactive` | Run the CLI directly from this shell. For `gcloud`, use `--no-launch-browser`, echo the login URL, ask for the code, and write it to stdin. | **no**, except asking for the auth code |
| `shell_install` | OS-level install (`brew install`, etc.). | **yes**, with the exact command shown |
| `human_action` | Cannot be fixed by the skill (Slack ping, IAM grant from a maintainer). | tell the user, don't loop |

## Workflow

### Step 1 — Install/bootstrap only when missing

If `gcloud_installed` is missing, ask before running the exact OS install
command from the setup report (`brew install --cask google-cloud-sdk` on macOS).

If the Powerpacks skill bundle or host adapter is missing and you are in the
Powerpacks repo, run the repo installer for the current host instead of trying
to repair it through a nested fix command:

```bash
./install.sh codex
./install.sh claude-code
```

Pick the host the user is currently using. If both hosts need repair, run both.
Because this writes outside the repo, request escalation if the sandbox
requires it.

### Step 2 — Login directly from this shell

If `auth0_login` is missing or expired, run:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/auth/auth.py login
```

If `gcloud_account` is missing, run:

```bash
gcloud auth login --no-launch-browser
```

Relay the full `https://accounts.google.com/...` URL from stdout to the user.
When `gcloud` asks for the verification code, ask the user for it and write it
to the running command's stdin. If `gcloud` cannot read or write
`~/.config/gcloud`, request escalation for `gcloud auth ...`.

If `gcloud_account` is a non-`@powerset.co` account, do not reject it only
because of the domain. The env pull path is per-user scoped and GCP Secret
Manager IAM is the source of truth. If matching per-user secrets are missing or
denied, surface that structured blocker.

Do not check or configure gcloud application-default credentials during the
normal login flow. Only run `gcloud auth application-default login
--no-launch-browser` if the user explicitly asks for SDK/client ADC debugging.

### Step 3 — Pull secrets and register MCP directly

If `env_file` is missing keys and `user_secrets` is accessible, run:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm \
  --best-effort
```

If `mcp_powerset_search` is missing but a host CLI exists, run:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

If `mcp_powerset_search` says no MCP host CLI is on PATH, that is an install
gap: use `./install.sh codex` or `./install.sh claude-code` only for the host
the user is using, or tell the user which host CLI is missing.

### Step 4 — Human-action blockers

**`human_action`** entries:

- `auth0_role` warn → "You're logged in but haven't been granted a Powerpacks
  role on Auth0. Ping #powerpacks on Slack with your @powerset.co email and
  ask to be added to the Powerpacks role."
- `user_secrets` `not_provisioned` / `not_privileged` → "You're set up
  locally but don't have GCP per-user secrets yet. Ping #powerpacks on
  Slack with the email you use for gcloud — a maintainer will run
  `provision_user_secrets apply --users <you>` and you can re-run
  `$powerset login`."

These are not blockers for unrelated workflows unless that workflow needs the
missing shared infra key.

### Step 5 — Final state

Re-run the setup check and report a terse final message:

- `overall: ok` → "Credentials updated. Please restart Codex to reload the
  Powerset MCP token."
- `overall: warn` → same, but mention the remaining warning in one short phrase.
- `overall: needs_setup` → list the still-blocked items and what the user
  needs to do.

## Hard rules

- **Never print secret values.** Every primitive redacts.
- **Never run an OS install command without explicit user approval.** That's
  the `shell_install` line.
- **Never write `.env` without `--confirm`.** All primitive paths refuse
  without it.
- Real authorization is enforced by GCP IAM on Secret Manager resources; do not
  add a separate email-domain gate in front of env pull.
- Per-user secret IDs follow `powerpacks-users-<slug>-<capability>`. The
  active `gcloud` account decides scope.

## Profiles

| Profile | Includes | When to pick |
| --- | --- | --- |
| `search-core` | Standard Powerpacks runtime secrets | default setup for search plus messages review/research |
| `search-network` | TurboPuffer + database + OpenAI only | minimal local `/search-network` setup |
| `import-contacts` | OpenRouter + Parallel only | minimal `$import-contacts` review/research setup |
| `messages` | OpenRouter + Parallel | `import-contacts` LLM/research extras |
| `sales-nav` | RapidAPI LinkedIn | LinkedIn enrichment |
| `twitter` | RapidAPI Twitter | Twitter pipelines |
| `supabase-admin` | Supabase URL + service role | admin only |
| `all` | every allowlisted key | one-shot full setup |

Pass `--profile <name>` to the setup check and to the direct provisioning
primitive.

## Maintainer-only: provisioning a new user

If a user shows up in `#powerpacks` with `user_secrets: not_provisioned`, a
maintainer (anyone with `roles/owner` on `powerset-search`) runs:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_user_secrets/provision_user_secrets.py plan \
  --users newperson@powerset.co \
  --project powerset-search --with-diff

uv run --project powerpacks python powerpacks/packs/powerset/primitives/provision_user_secrets/provision_user_secrets.py apply \
  --users newperson@powerset.co \
  --project powerset-search --confirm
```

Idempotent. Re-running on an existing user reconciles missing IAM bindings
or refreshes secret versions when the source value changed.
