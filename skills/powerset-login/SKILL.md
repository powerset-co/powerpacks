---
name: powerset-login
description: Log in to Powerset with Auth0 and provision local Powerpacks runtime secrets for Powerset employees, including TurboPuffer, Postgres/Supabase, OpenAI, OpenRouter, Parallel, and RapidAPI keys.
---

# Powerset Login

Use this skill when the user asks for `$powerset-login`, secret provisioning,
runtime setup, API key bootstrap, or checking whether Powerpacks has the
credentials needed to operate.

## Security Model

- Never print secret values.
- Only write allowlisted env keys through `provision_runtime_env`.
- Require Powerset Auth0 login before pulling secrets.
- Reject non-`@powerset.co` accounts locally.
- Treat local email checks as UX guardrails only. Real authorization must be
  enforced by search-api or GCP IAM.
- Prefer per-user search-api provisioning when available. GCP Secret Manager is
  an internal/team bootstrap fallback and does not by itself provide per-user
  usage attribution.

## Recommended Key Strategy

Use search-api as the long-term provisioning source:

- user authenticates through Auth0
- backend checks Powerset org membership and role
- backend returns scoped runtime credentials or short-lived delegated tokens
- backend logs who requested which capability
- backend can attach usage attribution to the user/operator

Use GCP Secret Manager only for trusted internal users while backend
provisioning is incomplete:

- GCP IAM controls who can access team secrets
- local primitive still requires Powerset login as an extra guardrail
- usage tracking is provider-key-level unless the provider supports per-user
  subkeys

Supabase/Postgres caveat: a shared `DATABASE_URL` cannot provide clean
per-user attribution. Prefer a backend-issued search API token or database role
with RLS if you need accountable user-level access. TurboPuffer support for
subkeys should be verified before promising user-level metering; otherwise
track usage at the Powerpacks/search-api layer.

## Workflow

1. Check auth:

```bash
python powerpacks/primitives/powerset_auth/powerset_auth.py whoami
```

2. If anonymous or expired, ask before opening a browser, then run:

```bash
python powerpacks/primitives/powerset_auth/powerset_auth.py login
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

Default source is `auto`, which tries search-api first and falls back to GCP.

Force search-api:

```bash
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --source search-api \
  --profile search-core \
  --env-file .env \
  --confirm
```

Force GCP:

```bash
POWERPACKS_GCP_PROJECT=powerset-prod \
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --source gcp \
  --profile search-core \
  --env-file .env \
  --confirm
```

Override a GCP Secret Manager id when the default mapping does not match:

```bash
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --source gcp \
  --secret TURBOPUFFER_API_KEY=my-secret-id \
  --env-file .env \
  --confirm
```

## Failure Handling

- If `whoami` returns anonymous, run login first.
- If pull fails with non-Powerset email, use `login --force-account`.
- If search-api returns 404, backend provisioning is not deployed yet; use GCP
  for internal testing.
- If GCP fails, check `gcloud auth login`,
  `gcloud auth application-default login`, project selection, and IAM.
