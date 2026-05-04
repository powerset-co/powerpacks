---
name: powerset-login
description: One-command Powerset login flow. Runs the doctor, auto-fixes everything safe to auto-fix, only stops to ask the user when an OS-level install or human action is genuinely required. Designed to feel like one click on a fresh box.
---

# Powerset Login

Use this skill when the user asks for `$powerset-login`, secret provisioning,
runtime setup, API key bootstrap, or "log me in to Powerset". Also the right
skill when an unrelated Powerpacks command failed because of a missing key
or expired session.

**This skill is built to be fast and quiet.** The user said "log me in" — do
that. Don't ask permission for every step. Use the doctor's `fix_kind`
classification to decide what needs asking and what doesn't.

## The two-command happy path

When everything is already in good shape (or only needs auto-fixes), this is
the entire flow:

```bash
# 1. Read-only check.
python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search

# 2. Run safe automatic fixes + browser-flow logins.
python powerpacks/packs/powerset/primitives/doctor/doctor.py fix --interactive \
  --profile search-core \
  --env-file .env \
  --gcp-project powerset-search
```

If `doctor fix` returns `after.overall == "ok"` (or `warn`), tell the user
"You're set up." and stop.

## How to handle the doctor's report

`doctor run` returns one JSON object. The two fields you care about are
`by_fix_kind` and `next_actions`. Each missing/fail check has a `fix_kind`:

| `fix_kind` | What to do | Ask the user? |
| --- | --- | --- |
| `auto` | Just run it. No network spend, no new credentials, no install. | **no** |
| `interactive` | Pops a browser the user clicks through (auth0 login, gcloud auth login). The browser dialog *is* the consent. | **no** |
| `shell_install` | OS-level install (`brew install`, etc.). | **yes**, with the exact command shown |
| `human_action` | Cannot be fixed by the skill (Slack ping, IAM grant from a maintainer). | tell the user, don't loop |

`doctor fix` (with `--interactive`) handles `auto` + `interactive` for you in
one pass. You only need to step in for `shell_install` and `human_action`.

## Workflow

### Step 0 — Run the doctor

```bash
python powerpacks/packs/powerset/primitives/doctor/doctor.py run \
  --profile search-core --env-file .env --gcp-project powerset-search
```

If `overall == "ok"`, tell the user "Already set up as `<email>`" and stop.

### Step 1 — Apply auto + interactive fixes in one shot

```bash
python powerpacks/packs/powerset/primitives/doctor/doctor.py fix --interactive \
  --profile search-core --env-file .env --gcp-project powerset-search
```

This will:

- Pull `.env` from the user's per-user GCP secrets (auto, no prompt)
- Register the `powerset-search` MCP into Claude Code and/or Codex
  (auto, no prompt; uses the cached Auth0 token)
- Pop a browser for Auth0 login if needed (browser is the consent)
- Pop a browser for `gcloud auth login` if needed (browser is the consent)
- Pop a browser for `gcloud auth application-default login` if needed

Tell the user once before running this: "I'm going to log you in. A browser
window may pop up for Auth0 and/or Google — sign in there." Then run it.

### Step 2 — Handle anything `doctor fix` skipped

The `skipped` list in `doctor fix`'s output contains everything that needs
the user beyond clicking a browser. Group them and ask **once** with all
relevant info — never one prompt per item.

**`shell_install`** entries (almost always `gcloud_installed` on a fresh
box):

> "I need to install `gcloud` to provision your `.env`. The command is:
> `brew install --cask google-cloud-sdk` (macOS) or
> `curl https://sdk.cloud.google.com | bash` (Linux). OK to run?"

After installation, re-run `doctor fix --interactive` — `gcloud auth login`
and `application-default login` will run as interactive steps in the same
pass.

**`human_action`** entries:

- `auth0_role` warn → "You're logged in but haven't been granted a Powerpacks
  role on Auth0. Ping #powerpacks on Slack with your @powerset.co email and
  ask to be added to the Powerpacks role."
- `user_secrets` `not_provisioned` / `not_privileged` → "You're set up
  locally but don't have GCP per-user secrets yet. Ping #powerpacks on
  Slack with your @powerset.co email — a maintainer will run
  `provision_user_secrets apply --users <you>` and you can re-run
  `$powerset-login`."

These are not blockers for unrelated workflows — the user still has a valid
Auth0 JWT for anything that doesn't require shared infra keys.

### Step 3 — Final state

Re-run `doctor run` and report a one-line summary:

- `overall: ok` → "Logged in as `<email>`. `.env` populated from per-user
  secrets (`<N>` keys)."
- `overall: warn` → same, but mention the optional `gcloud_adc` warn if
  present.
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
| `search-core` | TurboPuffer + Postgres + OpenAI | `/search-network`, `/search-company` |
| `messages` | OpenRouter + Parallel | `import-contacts-review` LLM/research |
| `sales-nav` | RapidAPI LinkedIn | LinkedIn enrichment |
| `twitter` | RapidAPI Twitter | Twitter pipelines |
| `supabase-admin` | Supabase URL + service role | admin only |
| `all` | every allowlisted key | one-shot full setup |

Pass `--profile <name>` to both `doctor run` and `doctor fix`.

## Maintainer-only: provisioning a new user

If a user shows up in `#powerpacks` with `user_secrets: not_provisioned`, a
maintainer (anyone with `roles/owner` on `powerset-search`) runs:

```bash
python powerpacks/packs/powerset/primitives/provision_user_secrets/provision_user_secrets.py plan \
  --users newperson@powerset.co \
  --project powerset-search --with-diff

python powerpacks/packs/powerset/primitives/provision_user_secrets/provision_user_secrets.py apply \
  --users newperson@powerset.co \
  --project powerset-search --confirm
```

Idempotent. Re-running on an existing user reconciles missing IAM bindings
or refreshes secret versions when the source value changed.
