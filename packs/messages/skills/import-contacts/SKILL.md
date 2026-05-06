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

Never upload contacts or run paid LLM/research steps without a separate,
explicit approval after showing the estimate.

## Checklist

Keep a visible task list and update it as work proceeds:

1. Check iMessage access
2. Import iMessage
3. Check Docker / WAHA
4. Link WhatsApp
5. Import WhatsApp
6. Merge contacts
7. Sync Powerset candidates
8. Match local contacts
9. Review unresolved contacts
10. Build enrichment queue
11. Estimate/run deep research when explicitly approved

Use `.powerpacks/messages/import-run.json` as the run ledger when practical.
Statuses: `pending`, `running`, `blocked_user_action`, `completed`, `failed`,
`skipped`.

## Workflow

1. Read `packs/messages/tasks/import-contacts.task.json`.
2. Run iMessage:
   - `extract_imessage_contacts.py check`
   - if readable, run `extract` to `.powerpacks/messages/imessage.contacts.*`
   - normalize to `.powerpacks/messages/imessage.contacts.normalized.jsonl`
3. Run WhatsApp:
   - `waha_runtime.py check`
   - if Docker is installed but stopped, ask before starting Docker/Colima
   - `waha_runtime.py up`
   - `waha_session.py start --open --wait`
   - if QR is needed, show the user the QR path and wait
   - `extract_whatsapp_contacts.py extract`
   - normalize to `.powerpacks/messages/whatsapp.contacts.normalized.jsonl`
4. Merge whichever sources exist:

```bash
python packs/messages/primitives/merge_message_contacts/merge_message_contacts.py merge \
  --input .powerpacks/messages/imessage.contacts.csv \
  --input .powerpacks/messages/whatsapp.contacts.csv \
  --output .powerpacks/messages/contacts.csv
```

Only include input files that exist.

5. Sync and match:

```bash
python packs/messages/primitives/sync_powerset_candidates/sync_powerset_candidates.py sync \
  --output .powerpacks/messages/powerset_contacts.csv

python packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv \
  --candidates .powerpacks/messages/powerset_contacts.csv
```

6. For review, prefer the local web editor:

```bash
python packs/messages/primitives/review_contacts_web/review_contacts_web.py serve \
  --contacts .powerpacks/messages/contacts.csv \
  --open
```

Use the web reviewer for yes/no enrichment decisions. A card click autosaves
`skip=false` for yes or `skip=true` for no. Do not ask the user to edit names,
match details, or free-text fields in the normal import flow. Use LLM review
only after showing the estimate and getting explicit approval.

7. Build the enrichment queue:

```bash
python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv
```

This queue uses the same name-quality and prune rules ported from
`../network-search-api/data_pipeline_v2/pipelines/synthetic/prepare_phone_contacts.py`:
only named, searchable, unresolved contacts with enough signal become paid
research candidates.

8. If the user explicitly asks to continue into deep research, estimate first:

```bash
python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.csv \
  --processor core2x
```

After the user approves the displayed Parallel.ai spend:

```bash
PARALLEL_API_KEY=... python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research
```

Then build and open the profile-card review:

```bash
python packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv

python packs/messages/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --open
```

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

End with a compact summary:

- source row counts
- merged unique contacts
- matched / suggested / unmatched counts
- review URL or artifact path
- research queue path and tier counts
- deep research estimate/path when run
