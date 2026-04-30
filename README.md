# Powerpacks

`powerpacks` is a portable recruiting-search pack: one user-facing skill,
deterministic primitives, schemas, and checked-in data contracts.

The core package is host-agnostic. It should work with NanoClaw, Codex, Claude
Code, or another agent runtime once that host knows how to expose the skill and
run the primitives.

## Goal

- make TurboPuffer and Postgres contracts explicit enough that agents do not
  guess field names, operators, or value types
- give the agent one operational search entrypoint: `/search-network <query>`
- decompose broad recruiting queries into bounded retrieval plans
- persist task state and CSV/JSONL artifacts so users can refine prior runs
- keep host-specific runtime glue isolated under `adapters/`

## Layout

- `skills/search-network/` is the only user-facing skill
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
installs `search-network` as the only skill, wires the threaded CLI channel,
and keeps NanoClaw-specific TUI/runtime code under `powerpacks/adapters/nanoclaw`.

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

## Development

```bash
bin/lint-powerpacks
bin/test-powerpacks
```

The lint command runs `ruff` and `flake8` through `uv` using the repo lockfile.

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
