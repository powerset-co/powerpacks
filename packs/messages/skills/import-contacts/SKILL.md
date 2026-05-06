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

Use the web editor for manual skip/match cleanup. Use LLM review only after
showing the estimate and getting explicit approval.

7. Build the enrichment queue:

```bash
python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv
```

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
