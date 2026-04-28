# Powerpacks

`powerpacks` is an overlay repo for installing schema-first search skills,
primitives, and runtime guidance into an existing NanoClaw checkout.

It is intentionally not a NanoClaw fork.

## Goal

- keep NanoClaw upstream clean and easy to update
- ship PowerSet-specific search skills and deterministic primitives separately
- install everything with one idempotent script
- make the TurboPuffer and Postgres contract explicit enough that agents do not
  guess wrong field names, operators, or value types
- make search planning explicit enough that agents do not default to one giant
  retrieval pass with no review strategy

## Layout

- `skills/` contains host-side skills copied into `.claude/skills/`
- `primitives/` contains deterministic scripts and wrappers
- `mcp/` contains MCP server scaffolds and notes
- `templates/` contains config fragments the installer can merge or copy
- `docs/` contains the public search surface and query rules
- `schemas/` contains JSON schemas for decomposed queries and filter shapes

## Install

```bash
./install.sh /path/to/nanoclaw
```

The installer currently:

- validates the target NanoClaw checkout
- copies `skills/` into `.claude/skills/`
- copies `primitives/` into `powerpacks/primitives/`
- copies template config into `powerpacks/templates/`
- copies `docs/` and `schemas/` into `powerpacks/`
- writes an install manifest for traceability

## Current Scope

V1 is intentionally narrow:

- one operational skill entrypoint: `$search-network <query>`
- query decomposition from natural language, job descriptions, or URLs
- multi-slice people search by role
- company criteria only as constraints inside role search
- explicit candidate-frontier review planning without expensive scoring
- TurboPuffer as the primary search surface
- Postgres as the hydration/supporting data surface

Excluded from the initial public surface:

- internal/private join logic
- Sales Nav
- repo-specific internal connector details
- broad enrichment workflows

Helper skill docs remain in this repo as internal references, but the install
step exposes one user-facing skill in NanoClaw: `search-network`.

## Next

- implement real wrappers around TurboPuffer MCP and Postgres MCP
- wire schema guidance into NanoClaw runtime config
- add package install steps for MCP dependencies
- add optional expensive scoring later, behind a separate primitive
