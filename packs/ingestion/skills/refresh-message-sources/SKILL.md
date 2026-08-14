---
name: refresh-message-sources
description: Refresh already-configured local Gmail, iMessage, and WhatsApp sources without onboarding or enrichment. Use for `$refresh-message-sources`, the `refresh-message-sources` Codex automation, recurring message-source refreshes, or requests to sync configured message sources and write the fixed source-state snapshot. Archives only the exact Codex automation task after finalization; interactive runs stay active.
---

# Refresh message sources

Refresh configured, healthy local message sources while preserving every source
that needs human setup. This is a thin workflow wrapper around
`$import-gmail sync` and `$import-messages sync`; those skills remain the source
of truth for source-specific readiness, discovery, and import behavior.

## Checklist

Create and execute this checklist with one item in progress at a time:

```
0. Identify automation context and rename the task
1. Refresh configured Gmail sources
2. Refresh configured Messages sources
3. Write and validate the source-state snapshot
4. Report outcomes and archive the automation task
```

## Workflow

1. Treat the run as the owned automation only when the current task input
   contains the exact metadata line:

   ```text
   Automation ID: refresh-message-sources
   ```

   Skill invocation, task title, working directory, and prior conversation
   history do not establish automation context.

2. In that exact automation context, immediately rename the current Codex task
   to `Refresh message sources MM/DD/YY` using the current local date. A rename
   failure is reportable metadata, not a reason to skip source work.

3. Load `import-gmail` and run only its explicit unattended
   `$import-gmail sync` contract. Select stored accounts automatically. Never
   create OAuth configuration, open a browser, authorize an account, widen the
   history window, or ask a setup question. Record a skipped outcome and
   preserve the prior Gmail import when human action is required.

4. Regardless of the Gmail outcome, load `import-messages` and run only its
   explicit unattended `$import-messages sync` contract. Include only channels
   whose existing readiness checks pass. Never open System Settings, install a
   helper, show a QR, authenticate, log out, re-link, or ask a setup question.
   Record skipped channels and preserve their prior imports.

5. This wrapper is source-only. Run the selected sources' configuration,
   discovery, local matching/import, and status work, but omit their fan-in and
   processing-suggestion steps. Never run Deep Context, message-body
   processing, an LLM, paid research, an upload, fan-in, or an index build.

6. Before returning on any terminal path, write the fixed snapshot from the
   canonical repo root:

   ```bash
   mkdir -p .powerpacks/automations/refresh-message-sources
   uv run --project . python packs/ingestion/primitives/imports/status.py status \
     --output .powerpacks/automations/refresh-message-sources/latest.json
   jq -e 'type == "object" and (.sources | type == "object")' \
     .powerpacks/automations/refresh-message-sources/latest.json >/dev/null
   ```

   If and only if the current checkout rejects `--output` as an unrecognized
   argument, capture the equivalent stdout to that same path and rerun the
   `jq` validation. Do not replace a valid prior snapshot with error output.

7. Report each configured source as refreshed or skipped, aggregate source-row
   and staged-candidate counts from current import manifests, the absolute
   snapshot path, and the skip reasons. Do not include message bodies, contact
   names, addresses, phone numbers, or account identifiers.

## Finalization and archive gate

Write the automation memory before archiving when the task supplies an
automation memory path.

- When Step 1 established the exact automation context, call
  `codex_app__set_thread_archived` with `archived: true` and omit `threadId` so
  it targets the current task. Call it immediately before the final response,
  after the snapshot and memory are durable. Then return the concise report.
- When the exact metadata line is absent, do not call any archive action. Leave
  interactive and manual skill runs active.
- If the archive action is unavailable or fails, still return the report and
  identify the archive failure. Never substitute `/archive`, `codex archive`,
  task deletion, or raw archive directives from inside a running task.
