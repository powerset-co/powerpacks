# import_contacts_pipeline

Resumable orchestrator for the messages import/contact enrichment flow.

This primitive is a mechanical task runner around the smaller messages
primitives. `run` starts a fresh import, archives prior contact/import artifacts
under `.powerpacks/messages/archive/`, exits at approval confirmations, and can be
resumed with `continue`.

## Commands

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py continue
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py approve
```

Callers can opt into only specific parts of the pipeline by passing explicit
include flags. When any include flag is present, only the selected phases run.
For example, setup's messages import worker prepares local message contacts
without Powerset sync, research, review, upload, or datalake sync:

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run \
  --include-imessage \
  --include-whatsapp \
  --include-contact-merge
```

Additional phases can be added by tacking on flags such as
`--include-powerset-candidates`, `--include-local-match`,
`--include-llm-review`, `--include-research`, `--include-review`,
`--include-retarget`, `--include-upload`, and `--include-datalake-sync`.

## Approval behavior

The orchestrator never reads approvals from stdin. When it reaches a spend or
upload confirmation, it exits with JSON like:

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
- Parallel.ai first-pass deep research always blocks on `approve` when there is
  paid work to submit; the user-facing block shows cost and rough time only.
- Retarget/correction research defaults to local Codex/Claude harness research
  for batches under 100 rows. Larger batches fall back to Parallel approval.
- Upload always blocks on `approve`; the user-facing block shows only
  the number of approved contacts.

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
10. Run the one-off final-schema migration so legacy buckets are canonical and
    existing Powerset matches are visible as `in_network`.
11. Start the local review web UI and block for the user to finish review.
12. On `continue`, detect saved `retarget_hint` feedback. If new hints exist,
    build `retarget_queue.csv`. For fewer than 100 rows, run local Codex/Claude
    harness retarget research and merge the results. For larger batches, estimate
    targeted Parallel research and block for approval.
13. Merge completed retarget results back into `research_review.csv`.
14. Summarize and block for upload approval.
15. Upload after approval.
