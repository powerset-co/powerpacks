---
name: import-contacts
description: One-command guided contact import workflow. Orchestrates iMessage, WhatsApp, merge, Powerset matching, research, review, retarget feedback, and upload gates.
---

# Import Contacts

Use this skill for `$import-contacts` or any request to import iMessage /
WhatsApp contacts into the Powerset messages research workflow.

## Rule

`$import-contacts` starts with a fresh run:

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run
```

After that, only use:

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py continue
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py approve
```

Do not ask the user to choose flags. Do not walk them through primitive
commands. Use primitives directly only for narrow debugging after the
orchestrator reports a concrete failure.

## Fresh Slate

`run` starts a new import and archives stale run-owned files first:

- active ledger
- merged contacts CSV
- channel exports and manifests
- Powerset candidate cache
- research queue
- review CSV
- retarget queue and ledger

It keeps expensive files:

- WhatsApp message-count cache for unchanged live chats
- deep research files, especially
`.powerpacks/messages/research/<handle>/01_research_parallel.json` and
`03_network_review.json`

So fresh runs rescan live WhatsApp but do not recount unchanged chats, and
already-researched handles are reused.
When a prior review CSV is archived, the regenerated review carries forward
explicit human decisions (`exclude`, `enrich_decision`) and `retarget_hint`
values for matching handles/phones.

`continue` resumes the active run and does not clear anything.

## Consent

Ask once before starting:

> This imports local message/contact metadata only. It never reads message
> bodies. It may read iMessage metadata, local Contacts names, sync WhatsApp,
> ask for a WhatsApp QR scan, match against your Powerset contacts, run paid
> research after approval, and ask again before upload. Continue?

After consent, do not ask again for local extraction, normalization, merge,
Powerset login, candidate sync, or matching. Stop only for:

- macOS Full Disk Access / Contacts permission
- Docker/WhatsApp QR action
- LLM cost approval when estimate is at least `$10.00`
- Parallel.ai research approval
- final upload approval

Never upload automatically.

## Execution

Use a worker sub-agent for the long-running orchestrator loop when available.
After consent, the main-chat handoff line should be exactly:

`Starting work through sub-agent.`

The main chat should show only:

- QR / permission actions
- spend prompts, with cost only
- upload prompts, with upload count only
- final result, exactly `Uploaded X contacts`

Do not stream primitive JSON, terminal transcripts, progress logs, local file
paths, row counts, matched/unmatched counts, or implementation details unless a
failure needs diagnosis or the user asks.

For WhatsApp, use plain user-facing status:

- `We're syncing WhatsApp.`
- `WhatsApp is taking a bit longer.`
- `WhatsApp needs a QR scan.`
- `WhatsApp sync finished.`

## Loop

1. Run `run`.
2. If blocked on a user action, tell the user the action, then run `continue`.
3. If blocked on spend/upload approval, ask the exact approval question. If the
   user approves, run `approve`, then `continue`.
4. If review opens, tell the user:
   `Review opened. When done, say: done with review, upload`
5. On review completion, run `continue`. Retarget feedback is automatic:
   edited `retarget_hint` rows are queued, researched after approval, merged
   back into the review CSV, then upload approval is requested.

## Output

Be terse.

- Spend: `Estimated deep research cost: $X, completion time is about Y. Approve?`
- Review: `Review opened. When done, say: done with review, upload`
- Upload: `Upload reviewed contacts? uploading X.`
- Done: `Uploaded X contacts`
