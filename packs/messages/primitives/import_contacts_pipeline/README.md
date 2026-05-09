# import_contacts_pipeline

Resumable orchestrator for the messages import/contact enrichment flow.

This primitive is a mechanical task runner around the smaller messages
primitives. `run` starts a fresh import, archives prior contact/import artifacts
under `.powerpacks/messages/archive/`, exits at approval gates, and can be
resumed with `continue`.

## Commands

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py continue
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py approve
```

## Approval behavior

The orchestrator never reads approvals from stdin. When it reaches a gate, it
exits with JSON like:

```json
{
  "status": "blocked_approval",
  "approval_type": "parallel",
  "approval_id": "parallel_abc123",
  "message": "Estimated deep research cost: $0.0500, completion time is about 10-15 min once submitted. Approve?",
  "continue_command": "uv run --project . python ... approve && uv run --project . python ... continue"
}
```

The agent asks the user that message. If the user approves, the agent runs the
printed `approve && continue` command.

Gates:

- OpenRouter LLM review auto-runs when the estimate is under `$10.00`.
  Otherwise it blocks on `approve`.
- Parallel.ai deep research always blocks on `approve` when there is
  paid work to submit; the user-facing block shows cost and rough time only.
- Upload always blocks on `approve`; the user-facing block shows only
  the number of yes/upload rows.

## Steps

1. On `run`, archive prior contact/import artifacts so stale channel exports,
   derived queues, review CSVs, and ledgers cannot be silently reused. The
   WhatsApp message-count cache, `01_research_parallel.json`, and
   `03_network_review.json` stay in place. Explicit review decisions plus
   retarget hints are carried forward from the archived review CSV. `continue`
   keeps the active ledger/artifacts.
2. Extract iMessage/Contacts.app rows and rescan live WhatsApp. The WhatsApp
   scan reuses cached counts for unchanged chats, then both channel exports are
   normalized.
3. Merge existing channel exports into `.powerpacks/messages/contacts.csv`, or
   create an empty canonical CSV if no channel export exists. If an existing CSV
   has incompatible headers, the pipeline fails fast with
   `packs/messages/schemas/contacts-csv.md` and asks the caller/agent to convert
   it before retrying.
4. Sync Powerset candidates.
5. Match local contacts.
6. Estimate/run LLM review if no completed review manifest exists.
7. Prepare `research_queue.csv`.
8. Estimate/run Parallel deep research after approval.
9. Build `research_review.csv`.
10. Start the local review web UI and block for the user to finish review.
11. On `continue`, detect saved `retarget_hint` feedback. If new hints exist,
    build `retarget_queue.csv`, estimate targeted Parallel research, and block
    for `approve` before upload.
12. Merge completed retarget results back into `research_review.csv`.
13. Summarize and block for upload approval.
14. Upload after approval.
