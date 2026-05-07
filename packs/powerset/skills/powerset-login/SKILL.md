---
name: powerset-login
description: One-command Powerset login flow. Uses the doctor as a read-only checker, self-heals missing setup from the current shell, and only stops for OS installs, visible auth codes, or human actions.
---

# Powerset Login

Use this skill when the user asks for `$powerset login`, `$powerset-login`,
secret provisioning, runtime setup, API key bootstrap, or "log me in to
Powerset". Also the right skill when an unrelated Powerpacks command failed
because of a missing key or expired session.

**This skill is built to be fast and quiet.** The user said "log me in" — do
that. Don't ask permission for every step. Use the doctor's `fix_kind`
classification to decide what needs asking, but run fixes yourself from the
current invocation/shell so prompts, URLs, and failures are visible.

## Core rule

`doctor.py run` is a checker. In the normal skill flow, do **not** run
`doctor.py fix`; it hides interactive work inside a nested subprocess and can
swallow browser/code prompts. Let the skill self-heal by running the relevant
primitive or CLI command directly.

Only use install commands when something is actually missing. Do not reinstall
Powerpacks adapters, MCP config, `gcloud`, or credentials when the doctor says
the check is already `ok`.

## Path setup

Resolve primitive paths from the current environment:

- From the Powerpacks repo root, use `packs/powerset/primitives/...`.
- From an installed skill bundle, use `powerpacks/packs/powerset/primitives/...`.
- If the example command path does not exist, locate it with `rg --files -g
  'doctor.py' -g 'auth.py' -g 'provision_runtime_env.py' -g 'mcp_install.py'`.

Prefer `python3` if `python` is not on PATH.

## Happy path

Run one read-only check:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

If `overall == "ok"`, tell the user "Already set up as `<email>`" and stop.
The doctor does not check gcloud application-default credentials by default;
ADC is not needed for normal Powerpacks workflows.

## How to handle the doctor's report

`doctor run` returns one JSON object. The fields you care about are `checks`,
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
command from the doctor (`brew install --cask google-cloud-sdk` on macOS).

If the Powerpacks skill bundle or host adapter is missing and you are in the
Powerpacks repo, run the repo installer for the current host instead of trying
to repair it through the doctor:

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

If `gcloud_account` is a non-`@powerset.co` account, do not guess. Tell the
user to log in as a Powerset account or run:

```bash
gcloud config set account you@powerset.co
```

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
  Slack with your @powerset.co email — a maintainer will run
  `provision_user_secrets apply --users <you>` and you can re-run
  `$powerset login`."

These are not blockers for unrelated workflows unless that workflow needs the
missing shared infra key.

### Step 5 — Final state

Re-run `doctor run` and report a one-line summary:

- `overall: ok` → "Logged in as `<email>`. `.env` populated from per-user
  secrets (`<N>` keys)."
- `overall: warn` → same, but mention the remaining warning.
- `overall: needs_setup` → list the still-blocked items and what the user
  needs to do.

## Hard rules

- **Never print secret values.** Every primitive redacts.
- **Never run an OS install command without explicit user approval.** That's
  the `shell_install` line.
- **Never write `.env` without `--confirm`.** All primitive paths refuse
  without it.
- The `@powerset.co` check is a UX guardrail; real authorization is enforced
  by GCP IAM on Secret Manager resources.
- Per-user secret IDs follow `powerpacks-users-<slug>-<capability>`. The
  active `gcloud` account decides scope.

## Profiles

| Profile | Includes | When to pick |
| --- | --- | --- |
| `search-core` | TurboPuffer + Postgres + OpenAI + Parallel | default setup for search plus messages deep research |
| `messages` | OpenRouter + Parallel | `import-contacts-review` LLM/research extras |
| `sales-nav` | RapidAPI LinkedIn | LinkedIn enrichment |
| `twitter` | RapidAPI Twitter | Twitter pipelines |
| `supabase-admin` | Supabase URL + service role | admin only |
| `all` | every allowlisted key | one-shot full setup |

Pass `--profile <name>` to `doctor run` and to the direct provisioning
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
