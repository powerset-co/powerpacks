# import_contacts_pipeline

Resumable orchestrator for the messages import/contact enrichment flow.

This primitive is a mechanical task runner around the smaller messages
primitives. It tracks progress in `.powerpacks/messages/import-run.json`, exits
at approval gates, and can be resumed with `continue`.

## Commands

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py continue
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py status
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py approve parallel --approval-id <id> --confirm
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py approve upload --approval-id <id> --confirm
```

## Approval behavior

The orchestrator never reads approvals from stdin. When it reaches a gate, it
exits with JSON like:

```json
{
  "status": "blocked_approval",
  "approval_type": "parallel",
  "approval_id": "parallel_abc123",
  "message": "Estimated Parallel cost: $0.0500. Approve?",
  "continue_command": "uv run --project . python ... approve parallel --approval-id parallel_abc123 --confirm && uv run --project . python ... continue"
}
```

The agent asks the user that message. If the user approves, the agent runs the
`approve ... --confirm` command and then `continue`.

Gates:

- OpenRouter LLM review auto-runs when the estimate is under `$10.00`.
  Otherwise it blocks on `approve llm`.
- Parallel.ai deep research always blocks on `approve parallel` when there is
  paid work to submit; the user-facing block shows cost only.
- Upload always blocks on `approve upload`; the user-facing block shows only
  the number of yes/upload rows.

## Steps

1. Ensure `.powerpacks/messages/contacts.csv` exists by reusing the unified CSV,
   merging existing channel exports, or creating an empty canonical CSV.
2. Sync Powerset candidates.
3. Match local contacts.
4. Estimate/run LLM review if no completed review manifest exists.
5. Prepare `research_queue.csv`.
6. Optimistically sync prior deep-research cache from GCS. If `gcloud rsync`
   fails, record a warning and continue with the local cache.
7. Estimate/run Parallel deep research after approval.
8. Build `research_review.csv`.
9. Start the local review web UI.
10. Summarize and block for upload approval.
11. Upload with `--confirm-upload` after approval.

## Useful flags

```bash
--stop-before-upload     # stop after opening review UI
--no-open-review         # build CSV but do not start the web UI
--rerun-llm              # ignore existing llm_review manifest
--llm-batch-size 20      # contacts per OpenRouter request
--llm-max-workers 4      # concurrent OpenRouter requests
--rerun-parallel         # rerun deep_research step; underlying primitive still skips existing handles
--rerun-upload           # allow another upload attempt even if ledger says completed
--force-sync-cache       # retry GCS cache sync
```
