# Powerpacks

`powerpacks` is an overlay repo for installing search- and enrichment-oriented
skills, primitives, and runtime config into an existing NanoClaw checkout.

It is intentionally not a NanoClaw fork.

## Goal

- keep NanoClaw upstream clean and easy to update
- ship PowerSet-specific skills and deterministic primitives separately
- install everything with one idempotent script

## Layout

- `skills/` contains host-side skills copied into `.claude/skills/`
- `primitives/` contains deterministic scripts and wrappers
- `mcp/` contains MCP server scaffolds and notes
- `templates/` contains config fragments the installer can merge or copy

## Install

```bash
./install.sh /path/to/nanoclaw
```

The installer currently:

- validates the target NanoClaw checkout
- copies `skills/` into `.claude/skills/`
- copies `primitives/` into `powerpacks/primitives/`
- copies template config into `powerpacks/templates/`
- writes an install manifest for traceability

## Next

- implement real primitive wrappers
- wire MCP servers into NanoClaw runtime config
- add package install steps for connector-specific dependencies
- add optional packs for Sales Nav and internal joins
