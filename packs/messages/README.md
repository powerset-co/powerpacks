# Messages pack

`packs/messages` provides the iMessage and WhatsApp metadata-import vertical for
the local Powerpacks search index.

Start with the
[iMessage and WhatsApp import pipeline](docs/message-import-pipeline.md). It is
the canonical product walkthrough for source access, matching, external provider
payloads, review, source fan-in, Modal indexing, resume behavior, and current
gaps.

## Skills

| Skill | Scope |
| --- | --- |
| [`$import-messages`](skills/import-messages/SKILL.md) | Full iMessage/WhatsApp flow: discover metadata, match local Gmail/LinkedIn people, research unresolved identities, mandatory review, import, fan-in, index, and validate. Never uploads contacts to a Powerset set. |
| [`$import-whatsapp`](skills/import-whatsapp/SKILL.md) | Isolated wacli readiness/auth/sync/export utility. Stops at the metadata CSV and does not resolve or index people. |

## Current runtime surface

- `extract_imessage_contacts`: read-only Messages/Contacts SQLite metadata.
- `import_whatsapp_wacli`: default WhatsApp provider setup, QR, local sync, and
  metadata export.
- `import_contacts_pipeline`: resumable extraction/merge orchestrator with
  structured user-action blocks.
- `match_local_candidates`: deterministic matching against existing local
  Gmail/LinkedIn people.
- `llm_review_contacts`: name-only OpenRouter enrichment triage.
- `deep_research_contacts`: approved Parallel.ai public-web identity research.
- `build_research_review_csv` and `review_research_web`: score and review
  researched contacts.
- `packs/ingestion/primitives/import_contacts_pipeline/messages.py`: materialize
  reviewed rows into the canonical Messages source `people.csv`.

WAHA/Docker primitives remain legacy fallback/testing surfaces. wacli is the
normal provider and should be the default in product docs and agent behavior.

## Privacy boundary

Powerpacks never selects message bodies in `$import-messages`. It reads contact
identity, counts, dates, source flags, and group metadata. wacli owns its local
provider store, so Powerpacks does not claim that store contains no bodies.
Approved metadata can leave the machine through the explicitly documented
OpenRouter, Parallel, RapidAPI, and Modal stages; see the product guide for exact
payloads.

Generated artifacts live under `.powerpacks/messages/` and
`.powerpacks/network-import/`. They are derivable and should not be committed.
