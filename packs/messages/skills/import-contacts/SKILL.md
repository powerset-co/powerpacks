---
name: import-contacts
description: One-command guided contact import workflow. Orchestrates iMessage, WhatsApp, merge, Powerset candidate sync, local matching, review, and enrichment queue prep with a resumable checklist.
---

# Import Contacts

Use this skill when the user wants to import contacts, import both iMessage and
WhatsApp, set up relationship signals, or run the full contacts harness.

This is the main user-facing entrypoint. The narrower `import-imessage`,
`import-whatsapp`, and `import-contacts-review` skills are subflows and
debugging escape hatches.

## Consent Model

Ask once at the beginning:

> This imports local message/contact metadata only. It never reads or stores
> message bodies. It may read local iMessage metadata, local Contacts names,
> start a local WAHA Docker container, ask you to scan a WhatsApp QR, merge CSVs,
> and sync your Powerset candidate catalog for local matching. Continue?

After the user says yes, do not ask again for local metadata extraction,
normalization, merge, Powerset login, candidate sync, or local matching. Stop
only for real human actions:

- macOS Full Disk Access / Contacts permission
- Docker install/start approval if Docker is missing or stopped
- WhatsApp QR scan
- LLM cost approval
- Parallel.ai/deep-research cost approval
- upload approval

Never upload contacts. The only upload step is the final reviewed research
artifact upload, and it requires a separate explicit approval after showing the
summary counts.

For OpenRouter LLM review/bucketing, always estimate first. If the estimate is
under `$10.00`, the initial workflow consent is enough; report only the cost and
proceed. If the estimate is `>= $10.00`, stop for explicit LLM cost approval.

For Parallel.ai deep research, always stop for explicit spend approval after the
estimate, even for small batches. Show only the cost, e.g. "Estimated Parallel
cost: $Z. Approve?"

## Fast path: resumable orchestrator

Prefer the orchestrator for normal runs. It is a mechanical task runner around
the primitives below and writes `.powerpacks/messages/import-run.json`.

Do **not** create a separate chat-visible plan for normal runs. After the user
gives the initial workflow consent, keep invoking the orchestrator until it
finishes or emits a concrete approval/user-action block. When blocked, show only
the concise block message (cost or action), collect the user's answer, then run
the printed `approve ... --confirm && continue` command or `continue` as
appropriate.

### Quiet execution

Keep the main chat quiet during long local stages. When the harness supports
sub-agents, dispatch the noisy execution to a worker sub-agent after initial
workflow consent. The worker should collect per-stage stats for debugging and
for the ledger, but the main agent should not show them by default.

The main chat should show only:

- required user actions, such as QR scan or OS permission steps
- spend prompts that require approval, with cost only
- upload prompts, with upload count only
- final upload result, exactly `Uploaded X contacts`

After local import/match/queue prep succeeds, use decision-oriented wording such
as `Imported contacts. LLM review estimated $X; continuing.` when the
OpenRouter estimate is under `$10.00`, or `Estimated Parallel cost: $Y.
Approve?` for Parallel. Do not include source row counts, matched/unmatched
counts, chat counts, candidate counts, or artifact paths in the main chat unless
the user asks for details or a failure requires diagnosis.

The worker may run the verbose terminal commands, poll sidecar progress files,
and inspect JSON manifests. Its final response must be one summary block per
stage with artifact paths and counts for the main agent to use internally. Do
not stream full primitive JSON, terminal transcripts, QR/WAHA status payloads,
or progress JSONL into the main chat unless a user action is required or a
failure needs diagnosis.

If sub-agents are unavailable, keep status messages decision-oriented and avoid
per-stage stats. Summarize command outputs from manifests internally instead of
narrating intermediate polling.

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run
```

It exits intentionally at approval gates and prints the exact question plus the
`approve ... --confirm && continue` command. Feed confirmations back with the
approval subcommand only after the user approves:

```bash
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py approve parallel \
  --approval-id <approval_id> --confirm
uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py continue
```

Use the same pattern for `approve upload`. GCS research-cache sync is
optimistic: if `gcloud storage rsync` fails, the orchestrator records a warning
and continues with the local `.powerpacks/messages/research` cache.

## Checklist

When running manually instead of through the orchestrator, keep a visible task
list and update it as work proceeds:

1. Check iMessage access
2. Import iMessage
3. Check Docker / WAHA
4. Link WhatsApp
5. Import WhatsApp
6. Merge contacts
7. Sync Powerset candidates
8. Match local contacts
9. Build enrichment queue
10. Sync existing deep-research cache from GCS into `.powerpacks/messages/research`
11. Estimate/run deep research when explicitly approved
12. Review profile cards / enrichment decisions
13. Upload reviewed artifact when explicitly approved

Use `.powerpacks/messages/import-run.json` as the run ledger when practical.
Statuses: `pending`, `running`, `blocked_user_action`, `completed`, `failed`,
`skipped`.

## Workflow

1. Read `packs/messages/tasks/import-contacts.task.json`. For the default path,
   run `import_contacts_pipeline.py run` and follow its approval blocks instead
   of manually dispatching every primitive.
2. Run iMessage:
   - `extract_imessage_contacts.py check`
   - if readable, run `extract` to `.powerpacks/messages/imessage.contacts.*`
   - normalize to `.powerpacks/messages/imessage.contacts.normalized.jsonl`
3. Run WhatsApp:
   - Tell the user: "I'll start a local WAHA Docker container. No message bodies
     are read. When the QR opens, use WhatsApp > Settings > Linked Devices >
     Link a Device. The exhaustive sync can take up to an hour; that's OK."
   - `waha_runtime.py check`
   - if Docker is installed but stopped, ask before starting Docker/Colima
   - `waha_runtime.py up`
   - `waha_session.py start --open --wait`
   - if QR is needed, show the user the QR path and wait; on timeout, run
     `waha_session.py wait` again instead of skipping WhatsApp
   - `extract_whatsapp_contacts.py extract` and keep message counts enabled.
     Do not add `--skip-message-counts` in normal runs. The primitive emits
     progress/heartbeat JSONL while it counts messages; let it run to completion.
   - normalize to `.powerpacks/messages/whatsapp.contacts.normalized.jsonl`
4. Merge whichever sources exist:

```bash
uv run --project . python packs/messages/primitives/merge_message_contacts/merge_message_contacts.py merge \
  --input .powerpacks/messages/imessage.contacts.csv \
  --input .powerpacks/messages/whatsapp.contacts.csv \
  --output .powerpacks/messages/contacts.csv
```

Only include input files that exist.

5. Sync and match:

```bash
uv run --project . python packs/messages/primitives/sync_powerset_candidates/sync_powerset_candidates.py sync \
  --output .powerpacks/messages/powerset_contacts.csv

uv run --project . python packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv \
  --candidates .powerpacks/messages/powerset_contacts.csv
```

6. Build the enrichment queue:

```bash
uv run --project . python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv
```

This queue uses the same name-quality and prune rules ported from
`../network-search-api/data_pipeline_v2/pipelines/synthetic/prepare_phone_contacts.py`:
only named, searchable, unresolved contacts with enough signal become paid
research candidates.

7. Sync already-researched profiles before estimating Parallel spend.

This uses the cached Powerset token to resolve the current operator and `gcloud
storage rsync` to download the operator-scoped processing cache into the local
Powerpacks research dir. For Arthur this should resolve to operator
`e33a648a-ae5f-432e-83ce-b90d75546ada` / `thearthurchen@gmail.com`.

```bash
uv run --project . python packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py status
uv run --project . python packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py download
```

Then estimate Parallel deep research. The estimate skips rows that already have
`.powerpacks/messages/research/<handle>/01_research_parallel.json`:

```bash
uv run --project . python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research
```

Stop here and ask for explicit Parallel spend approval. After the user confirms:

```bash
uv run --project . python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research
```

8. Build and open the profile-card review:

```bash
uv run --project . python packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv

uv run --project . python packs/messages/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --open
```

This is the default review surface after Parallel runs. It shows the profile
data from `01_research_parallel.json` and autosaves yes/no decisions to the
`exclude` column in `research_review.csv`.

After opening the review UI, tell the user: "When you're done reviewing, say
'done with review, upload'. I'll check feedback first, then ask for explicit
approval before syncing anything." After review, first run
`prepare_retarget_queue prepare` to detect saved feedback hints. If it writes
rows, tell the user that you'll run one targeted Parallel pass for the feedback
rows before upload, estimate the cost, and ask for Parallel approval using cost
only. If Parallel fails, is unavailable, or returns no plausible person for a
feedback row, automatically run a small Codex web-search fallback for just those
feedback rows and save separate retarget artifacts. After feedback/retarget
handling, ask for upload approval using only the number of yes rows that will be
uploaded. Make clear that nothing has been uploaded yet:

```bash
uv run --project . python packs/messages/primitives/upload_research_review/upload_research_review.py summarize \
  --csv .powerpacks/messages/research_review.csv
```

Only after the user explicitly approves the upload:

```bash
uv run --project . python packs/messages/primitives/upload_research_review/upload_research_review.py upload \
  --csv .powerpacks/messages/research_review.csv \
  --confirm-upload
```

This posts to `/v2/messages-research/artifacts`. The server stores a reviewed
artifact with yes/maybe/no splits; the yes split is the include/enrich set. The
primitive translates the web UI's `exclude` decisions into upload buckets so
explicit yes/no enrich choices are reflected in that split.

Then upload the reviewed rows plus joined deep-research profiles to Powerset:

```bash
uv run --project . python packs/messages/primitives/sync_contact_datalake/sync_contact_datalake.py sync \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --confirm-sync
```

This posts to `/v2/contact-datalake/import` and is covered by the same explicit
final upload/sync approval. Report the result to the user as only:
`Uploaded X contacts`.

If Parallel is skipped, unavailable, or the queue is empty, fall back to the raw
contacts yes/no reviewer:

```bash
uv run --project . python packs/messages/primitives/review_contacts_web/review_contacts_web.py serve \
  --contacts .powerpacks/messages/contacts.csv \
  --open
```

Use the web reviewer for yes/no enrichment decisions only. Do not ask the user
to edit names, match details, or free-text fields in the normal import flow.
Use LLM review only after showing the estimate; OpenRouter estimates under
`$10.00` may proceed without another approval, while anything else requires
explicit LLM cost approval.

If `PARALLEL_API_KEY` is unavailable and the user still wants review help,
fall back to parallel sub-agent review over small queue shards. Each sub-agent
should return only public LinkedIn/profile candidates plus a confidence and
reason; never send message bodies.

## Resume Rules

- If iMessage already produced `imessage.contacts.csv`, do not re-extract
  unless the user asks.
- If WAHA session is `WORKING`, do not show a QR again.
- If `contacts.csv` exists, merge can be rerun safely.
- If `powerset_contacts.csv` exists and sync fails, use the cached catalog and
  continue to local matching.
- If a step blocks on user action, report the exact action and the command to
  continue.

## Output

End with only the next action or final result:

- if blocked on spend: `Estimated <provider> cost: $X. Approve?`
- if review is ready: `Review opened. When done, say: done with review, upload`
- if upload is approved and complete: `Uploaded X contacts`
- include detailed stats only if the user explicitly asks or a failure needs diagnosis
