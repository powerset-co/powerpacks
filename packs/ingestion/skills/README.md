# Ingestion skills and handlers

Skills are user-facing routing/instruction files. They do not ingest data by
themselves; each skill chooses the primitive/orchestrator script that performs
the work. Discovery owns source extraction. Merge, network DuckDB
materialization, and local search indexing are owned by
`packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py`.

| User command / skill | Skill file | Primary script called | What it handles | Output contract |
| --- | --- | --- | --- | --- |
| `$import-email` | `packs/ingestion/skills/import-email/SKILL.md` | `packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run --gmail-account-email ...` | Local msgvault Gmail/email metadata discovery | Gmail source folder |
| `$discover-contacts` | `packs/ingestion/skills/discover-contacts/SKILL.md` | `packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run ...` | Source discovery across LinkedIn CSV, msgvault email, and optionally existing message/Twitter artifacts | Fixed source folders |
| `$import-twitter` | `packs/ingestion/skills/import-twitter/SKILL.md` | `packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run ...` | Twitter/X crawl, MOE, and LinkedIn validation smoke/import | Twitter source `people.csv` |
| `$import-messages` | `packs/messages/skills/import-messages/SKILL.md` | messages pack primitives | iMessage/WhatsApp contact metadata import/review | Message contacts artifacts |
| LinkedIn CSV inside `$discover-contacts` | `packs/ingestion/skills/discover-contacts/SKILL.md` | `packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py` called by orchestrator | LinkedIn `Connections.csv`, enrichment cache/API approval | LinkedIn source `people.csv` |

## Why skills are not the runtime handler

A skill is the harness-facing contract: when the user says `$import-email`, the
agent loads `SKILL.md` and follows its instructions. The runtime handler is the
Python primitive/orchestrator because it needs deterministic CLI arguments,
ledger/resume state, JSON output, tests, and child approval confirmations.

So the flow is:

```text
user command → skill routing/instructions → primitive/orchestrator → artifacts
```

For end-to-end local search readiness tests use `index_contacts_pipeline.py run`;
it proves source fan-in, merge contracts, `network_companies.csv`, network
DuckDB, processing records, and local search DuckDB work together.
