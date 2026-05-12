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

```text
Import contacts

- Imports only local iMessage and WhatsApp message/contact metadata; message bodies are never read.
- Reads contact names for matching.
- WhatsApp may require a QR scan to link your device.
- Uses Powerset Contacts to match your iMessage/WhatsApp contacts.
- Runs paid research only after you approve it.

**Your explicit permission will be required to share data after completing import and reviews.**

Continue?
```

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

The worker handoff must explicitly say consent was already granted in the main
thread, and that the worker must not ask for consent again. Give it the exact
orchestrator command to run.

The main chat should show only:

- QR / permission actions
- spend prompts, with cost only
- upload prompts, with approved contact count only
- final result, exactly `Uploaded X approved contacts`

Do not stream primitive JSON, terminal transcripts, progress logs, local file
paths, row counts, matched/unmatched counts, or implementation details unless a
failure needs diagnosis or the user asks.

The terminal may show terse primitive progress lines while long steps run. That
is expected; do not paste every line into chat. Use those lines only to confirm
the process is alive or diagnose a real stall.

For WhatsApp, use plain user-facing status:

- `We're syncing WhatsApp.`
- `WhatsApp is taking a bit longer.`
- `WhatsApp needs a QR scan.`
- `WhatsApp sync finished.`

## Timing

Do not treat long-running stages as failures just because they are quiet.

- iMessage, merge, match, and queue prep are usually seconds to a few minutes.
- WhatsApp live sync can take several minutes. Large accounts can take much
  longer; the primitive writes heartbeat/progress events while counting changed
  direct chats.
- LLM skip/enrich review auto-runs under the cost threshold and can take
  minutes on thousands of named unmatched contacts.
- Parallel deep research uses the estimate block as the timing source. For
  `core2x`, expect about 10-15 minutes after approval.
- Network-review scoring writes `03_network_review.json` cache files and can
  take minutes when many new researched profiles need scoring.

At 10k contacts, expect local steps to remain viable but LLM and research gates
to dominate. At 100k contacts, do not assume the current one-shot flow is the
right path; pause and inspect counts/estimates before approving spend.

## Loop

1. Run `run`.
2. If blocked on a user action, tell the user the action, then run `continue`.
3. If blocked on spend/upload approval, ask the exact approval question. If the
   user approves, run `approve`, then `continue`.
4. If review opens, tell the user:
   `Review opened: <url>. When done, say: done with review, upload`
5. On review completion, run `continue`. Retarget feedback is automatic:
   edited `retarget_hint` rows are queued, researched after approval, merged
   back into the review CSV, then upload approval is requested.

## Output

Be terse.

- Spend: `Estimated deep research cost: $X, completion time is about Y. Approve?`
- Review: `Review opened: <url>. When done, say: done with review, upload`
- Upload: `Upload approved contacts? uploading X.`
- Done: `Uploaded X approved contacts`
