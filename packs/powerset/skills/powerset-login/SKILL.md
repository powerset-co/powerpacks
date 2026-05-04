---
name: powerset-login
description: Set up local Powerpacks runtime secrets from GCP Secret Manager for Powerset employees, including TurboPuffer, Postgres/Supabase, OpenAI, OpenRouter, Parallel, and RapidAPI keys.
---

# Powerset Login

Use this skill when the user asks for `$powerset-login`, secret provisioning,
runtime setup, API key bootstrap, or checking whether Powerpacks has the
credentials needed to operate.

## Security Model

- Never print secret values.
- Only write allowlisted env keys through `provision_runtime_env`.
- Require active `gcloud` auth before pulling secrets.
- Reject non-`@powerset.co` active gcloud accounts locally.
- Treat local email checks as UX guardrails only. Real authorization must be
  enforced by GCP IAM on Secret Manager resources.
- Do not use search-api for provisioning.

## Recommended Key Strategy

Use GCP Secret Manager as the source of truth:

- user authenticates with Google via `gcloud auth login`
- GCP IAM checks whether that user can access each secret
- Powerpacks pulls allowlisted secrets directly from Secret Manager
- Powerpacks writes the local `.env` with mode `0600`
- Powerpacks output only includes redacted metadata

User-scoped keys should be represented as GCP resources, not a server-side
lookup table:

- create one secret resource per user/capability when a provider supports
  per-user keys
- grant `roles/secretmanager.secretAccessor` only to the intended user or group
- use naming such as `powerpacks/users/<email>/<capability>` conceptually, but
  remember Secret Manager secret IDs are flat resource IDs, not filesystem
  paths
- prefer labels/annotations for owner, capability, provider, and environment
- rotate by replacing the user's secret version or deleting the user secret

Supabase/Postgres caveat: a shared `DATABASE_URL` cannot provide clean
per-user attribution. If you need accountable user-level access, issue
per-user database roles/connection strings or use RLS. TurboPuffer support for
subkeys should be verified before promising user-level metering; otherwise
usage is attributable only to the shared key or to Powerpacks-side logs.

## Workflow

1. Check Google auth:

```bash
gcloud auth list --filter=status:ACTIVE
```

2. If needed, ask before opening a browser, then run:

```bash
gcloud auth login
```

3. Show the provisioning plan:

```bash
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py plan \
  --profile search-core \
  --env-file .env
```

4. Pull secrets only after the user explicitly asks to provision:

```bash
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm
```

5. Validate:

```bash
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py check \
  --profile search-core \
  --env-file .env
```

## Profiles

- `search-core`: `TURBOPUFFER_API_KEY`, `TURBOPUFFER_REGION`,
  `DATABASE_URL`, `OPENAI_API_KEY`
- `messages`: `OPENROUTER_API_KEY`, `PARALLEL_API_KEY`
- `sales-nav`: `RAPIDAPI_KEY`
- `supabase-admin`: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `all`: every allowlisted key

Use `--include KEY` for one-off additions.

## Source Selection

There is only one source: GCP Secret Manager.

```bash
POWERPACKS_GCP_PROJECT=powerset-prod \
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm
```

Override a GCP Secret Manager id when the default mapping does not match:

```bash
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --secret TURBOPUFFER_API_KEY=my-secret-id \
  --env-file .env \
  --confirm
```

## Failure Handling

- If pull reports no active gcloud account, run `gcloud auth login`.
- If pull fails with non-Powerset email, switch gcloud accounts.
- If GCP fails, check `gcloud auth login`,
  `gcloud auth application-default login`, project selection, and IAM.
