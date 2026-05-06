# Messages Pack

`packs/messages` is a local-first import harness for relationship signals from
iMessage and WhatsApp. No `contact-exporter` dependency for either channel.

The pack uses bare, inspectable, stdlib-only primitives. The current boundary
is:

- iMessage: local SQLite reads only, single primitive
- WhatsApp: small primitives that own Docker lifecycle, WAHA session/QR auth,
  and contact extraction independently
- Powerpacks owns task state, primitive contracts, schemas, normalization,
  manifests, and agent-facing workflow instructions
- the harness captures local failures as repair artifacts so an agent can
  patch a primitive for the machine in front of it

## Primitive Surface

iMessage:

- `extract_imessage_contacts`: read local macOS Messages/Contacts SQLite
  metadata with Python stdlib only

WhatsApp (all stdlib-only, all gated on explicit user consent):

- `waha_runtime`: Docker check + WAHA NOWEB container lifecycle
- `waha_session`: WAHA session start/stop + QR PNG/text artifacts + auth poll
- `extract_whatsapp_contacts`: pull contacts from an authenticated WAHA
  session into the canonical CSV/JSONL shape

Cross-channel:

- `import_contacts_pipeline`: resumable orchestrator that runs the mechanical
  import/match/review/research/upload sequence, tracks
  `.powerpacks/messages/import-run.json`, and exits at approval gates
- `messages_harness`: run message primitives tolerantly and emit repair notes
- `normalize_message_contacts`: convert pack CSV output into a canonical JSONL
  artifact and summary manifest
- `merge_message_contacts`: dedupe and union N per-channel CSVs into a single
  `contacts.csv` (e.g. iMessage + WhatsApp → unified)
- `prepare_research_queue`: filter + reshape `contacts.csv` into the
  deep-research input CSV (with priority tiers and per-processor cost estimates)
- `sync_messages_research_cache`: download operator-scoped prior deep research
  from the processing GCS bucket into `.powerpacks/messages/research` before
  spending new Parallel credits
- `deep_research_contacts`: run Parallel.ai deep research over the queue and
  write per-handle `01_research_parallel.json` artifacts; native HTTP port of
  aleph-mvp's `research_parallel.py` so Powerpacks does not depend on the
  `parallel` SDK
- `build_research_review_csv`: flatten the per-handle research artifacts into
  a single CSV in the shape `contact-exporter`'s research-review TUI consumes
  (`bucket / yes-maybe-no` view) and `/v2/messages-research/artifacts` accepts
  on upload
- `review_research_web`: local browser port of the research-review TUI with
  yes/maybe/no tabs, profile cards, and autosaved yes/no enrichment decisions
- `upload_research_review`: upload the reviewed research CSV to
  `/v2/messages-research/artifacts` after explicit approval, translating the
  UI's `exclude` decisions into server upload buckets
- `powerset_contacts_harness`: optional compatibility shim for non-WhatsApp
  channels of `contact-exporter` (review/match-local/upload). Not used by the
  WhatsApp skill.
- `review_contacts_web`: local browser yes/no enrichment reviewer on the merged
  contacts CSV, with tabs for matched, suggested, actionable unmatched,
  low-signal, and skipped rows

## Skills

- `import-contacts`: one-command guided iMessage + WhatsApp import, merge,
  candidate sync, local matching, web review, queue prep, and optional
  Parallel deep research after cost approval
- `import-imessage`: local-only iMessage extraction and normalization
- `import-whatsapp`: WhatsApp extraction via the three WAHA primitives above
- `import-contacts-review`: Powerset login + candidate sync + local matching +
  LLM ENRICH/SKIP review on the imported contacts CSV

## Harness Stance

Extraction is local and consentful. The harness can prepare and record
commands, but an agent should not run iMessage, WhatsApp, Docker install, QR
auth, extraction, Parallel paid research, or upload actions unless the user has
explicitly asked for that action in the current task. OpenRouter Sonnet review
under `$1.00` may proceed after showing the estimate; all Parallel research and
all uploads require explicit approval.

Generated artifacts live under `.powerpacks/messages/` by default.
