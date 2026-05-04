# Powerpacks

`powerpacks` is a portable recruiting-search pack: one user-facing skill,
deterministic primitives, schemas, and checked-in data contracts.

The core package is host-agnostic. It should work with NanoClaw, Codex, Claude
Code, or another agent runtime once that host knows how to expose the skill and
run the primitives.

## Goal

- make TurboPuffer and Postgres contracts explicit enough that agents do not
  guess field names, operators, or value types
- give the agent operational entrypoints: `/search-network <query>`,
  `/search-company <query>`, and `$powerset-login`
- decompose broad recruiting queries into bounded retrieval plans
- persist task state and CSV/JSONL artifacts so users can refine prior runs
- keep host-specific runtime glue isolated under `adapters/`

## Layout

- skills: `skills/search-network/`, `skills/extract-search-query/`,
  `skills/search-company/`, `skills/powerset-login/`,
  `packs/messages/skills/import-imessage/`,
  `packs/messages/skills/import-whatsapp/`, and
  `packs/messages/skills/import-contacts-review/`
- `primitives/` contains deterministic scripts and primitive contracts
- `primitives/lib/` contains shared runtime contracts and service clients
- `schemas/` contains JSON schemas for query, filter, task, and result shapes
- `contracts/` contains checked-in Postgres, TurboPuffer, and hydrated-profile
  contracts
- `tasks/` contains plan/execute task templates
- `packs/` contains optional domain packs with their own skills, primitives,
  schemas, tasks, and docs
- `docs/workflows/` contains helper workflow references previously exposed as
  `add-*` skills
- `evals/` contains lightweight plan cases
- `adapters/` contains host-specific installers, runtime patches, CLIs, and
  harnesses

## Install

NanoClaw adapter:

```bash
./install.sh nanoclaw /path/to/nanoclaw
```

Codex adapter:

```bash
./install.sh codex
```

This installs the Powerpacks skills into `${CODEX_HOME:-~/.codex}/skills`.
Restart Codex after installing so the skill list is reloaded.

Direct adapter installs also work:

```bash
./adapters/nanoclaw/install.sh /path/to/nanoclaw
./adapters/codex/install.sh
```

The NanoClaw adapter copies the core Powerpacks directory into the target,
installs `search-network` and its `extract-search-query` sub-skill, wires the
threaded CLI channel, and keeps NanoClaw-specific TUI/runtime code under
`powerpacks/adapters/nanoclaw`.

Claude Code adapter is intentionally not implemented yet.

## Contracts

Powerpacks treats Postgres and TurboPuffer schema as checked-in contracts, not
something the agent should rediscover on each run:

```bash
python powerpacks/primitives/contracts/contracts.py list
python powerpacks/primitives/contracts/contracts.py check-postgres --env-file .env
python powerpacks/primitives/contracts/contracts.py dump-postgres --env-file .env --out .powerpacks/schema-dumps/postgres-live.json
```

`dump-postgres` writes a diagnostic artifact. It does not mutate the checked-in
contracts.

## Runtime Provisioning

Internal Powerset users can provision a local `.env` from GCP Secret Manager
without pasting raw secrets into chat:

```bash
gcloud auth login
python powerpacks/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm
```

The provisioning primitive redacts secret values in output and only writes
allowlisted keys. Authorization is enforced by GCP IAM on Secret Manager
resources. For user-scoped keys, create per-user/per-capability secrets and
grant access on those specific secret resources or groups.

## Task Flow

See `docs/task-flow.md` for the current search task lifecycle, the extraction
sub-skill boundary, and the difference between primitive parity harnesses and
agent extraction harnesses.

## Development

```bash
scripts/lint-powerpacks
scripts/test-powerpacks
```

The lint command runs `ruff` and `flake8` through `uv` using the repo lockfile.

## Testing

Use `scripts/test-search-network check` for local readiness. For headless Codex
extraction and recall harness details, see `docs/testing.md`.

## Current Scope

V1 is intentionally narrow:

- query decomposition from natural language, job descriptions, or URLs
- role-first people search with optional company constraints
- recall-style filters: education, tenure, years of experience, age, seniority
- company-side filters: headcount, funding, valuation, founded year, sector,
  entity type, company geography
- company-domain adjacency only when requested, confirmed, or explicitly
  planned as an exploratory slice
- TurboPuffer as the primary search surface
- Postgres as the hydration/supporting data surface
- conservative LLM filtering after full-frontier hydration
- CSV/JSONL/manifest artifact persistence for refinement

Excluded from the public V1 surface:

- internal/private join logic
- Sales Nav
- repo-specific internal connector details
- broad enrichment workflows
- company summary or company-signal search
- expensive scoring/reranking

## Adapter Notes

NanoClaw-specific pieces now live under `adapters/nanoclaw/`:

- `install.sh` installs Powerpacks into a NanoClaw checkout
- `runtime/` contains the threaded CLI channel and container patches
- `bin/powerclaw` is the legacy NanoClaw terminal wrapper
- `primitives/view_search_results/` is the legacy NanoClaw TUI
- `primitives/nanoclaw_plan_harness/` is the NanoClaw plan-only harness

Those pieces are not part of the portable primitive surface.
