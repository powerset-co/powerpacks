---
name: build-outbound
description: Script-backed Apollo outbound workflow. Use for `$build-outbound setup`, `$build-outbound status`, `$build-outbound prepare-leads`, previewing copy from Sales Nav leads, creating inactive Apollo sequences, and activation after exact campaign confirmation.
---

# Build Outbound

Use this skill when the user asks to turn `$sales-nav-search` leads into an Apollo outbound sequence, or asks for Apollo setup/status. The sequence build is backed by `packs/apollo/primitives/build_outbound/build_outbound.py`; setup/status stay on `packs/apollo/primitives/apollo_mcp/apollo_mcp.py`.

## Prerequisites

- `APOLLO_API_KEY` in `.env` or the shell. Use a Master API key for sequence/email-account endpoints.
- A connected Apollo sending mailbox and an Apollo emailer schedule.
- A Sales Nav run under `.powerpacks/sales-nav/runs/` or an explicit Sales Nav `manifest.json` / `state.json`.
- Never paste API keys, full lead lists, raw Apollo responses, or unmasked emails into chat.

## Setup and status routes

```bash
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py status --host codex
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py install --host codex
# add --host claude or --host all when requested
```

- `$build-outbound status`: run status and report only whether key, Node/npx, and MCP registration are present. Never print the key.
- `$build-outbound setup`: if `APOLLO_API_KEY` exists, run install for the requested host; otherwise tell the user how to add it locally.
- `$build-outbound prepare-leads`: legacy/manual handoff route; run `apollo_mcp.py prepare-leads --input <export.csv>` and report counts/paths.

## Normal `$build-outbound <instructions>` route

1. Resolve the Sales Nav input:

   ```bash
   uv run --project . python packs/apollo/primitives/build_outbound/build_outbound.py resolve-sales-nav \
     --query-hint "<user instructions>"
   # or include --sales-nav-manifest <path> / --state <path> when the user points to one
   ```

2. Print the selected search query, state/manifest path, lead count, and exactly: “If this is the wrong search, point me to the right manifest/state.”

3. Draft or use reviewed copy, then create a local preview:

   ```bash
   uv run --project . python packs/apollo/primitives/build_outbound/build_outbound.py preview \
     --instructions "<user instructions>" \
     --query-hint "<user instructions>"
   ```

   This writes `sequence_input.json` and `sequence_preview.md`. Print the exact subject/body preview for every step.

4. Preview checkpoint: because enrichment spends Apollo credits and build mutates Apollo (sequence/contact creation and enrollment), ask for “proceed with build” confirmation unless the original request unambiguously asked to build/create/enrich now.

5. Build the inactive Apollo campaign from the reviewed copy:

   ```bash
   uv run --project . python packs/apollo/primitives/build_outbound/build_outbound.py build \
     --instructions "<user instructions>" \
     --query-hint "<user instructions>" \
     --sequence-json <reviewed sequence_input.json>
   ```

   Use `--sales-nav-manifest <path>` or `--state <path>` instead of `--query-hint` when applicable. The build discovers default sender/schedule unless explicit IDs are supplied. Report campaign id, counts, masked email counts, and local artifact paths under `.powerpacks/apollo/build-outbound/<run>/`.

6. Activation is separate. Do not run activation until the user explicitly confirms activation for the exact returned `campaign_id`.

## Activation command

Only after exact campaign confirmation, run:

```bash
uv run --project . python packs/apollo/primitives/build_outbound/build_outbound.py activate \
  --manifest .powerpacks/apollo/build-outbound/<run>/manifest.json \
  --confirm-activation <campaign_id>
```

The command requires the confirmation id to match the manifest campaign id, activates through Apollo, polls message status, and writes `activation_status.json`.

## Safety rules

- Do not run live Apollo mutations without explicit user approval: enrichment, contact create/dedupe, sequence/campaign creation, contact enrollment, and activation are spend-bearing or mutating.
- Preview and resolve commands are safe local/read-only steps.
- Non-dry-run build creates an inactive campaign only; it must not activate sending.
- `activate` is the only route that can approve/activate the campaign, and it requires exact campaign id confirmation.
- Use `--dry-run` for local smoke checks. Do not run live activation tests.
- Console output must keep API keys redacted and emails masked; raw Apollo artifacts stay local under `.powerpacks/`.
