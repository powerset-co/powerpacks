# Ingestion skills and handlers

Skills are user-facing routing/instruction files. They do not ingest data by
themselves; each skill chooses the primitive/orchestrator script that performs
the work. The orchestrator keeps end-to-end runs consistent by calling the same
source primitives, merge step, and DuckDB loader every time.

| User command / skill | Skill file | Primary script called | What it handles | Output contract |
| --- | --- | --- | --- | --- |
| `$import-email` | `packs/ingestion/skills/import-email/SKILL.md` | `packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --gmail-account-email ...` | Local msgvault Gmail/email metadata import, then merge + DuckDB | Gmail `people.csv`; merged `people.csv`, `network_contacts.csv`, `network_contact_sources.csv`, `network_companies.csv`; DuckDB |
| `$import-network` | `packs/ingestion/skills/import-network/SKILL.md` | `packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run ...` | End-to-end local network orchestration across LinkedIn CSV, msgvault email, and optionally existing message/Twitter artifacts | Same merged CSVs + DuckDB |
| `$import-twitter` | `packs/ingestion/skills/import-twitter/SKILL.md` | `packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run ...` | Twitter/X crawl, MOE, and LinkedIn validation smoke/import | Twitter run `people.csv`; merge through `$import-network --include-existing-artifacts` |
| `$import-contacts` | `packs/messages/skills/import-contacts/SKILL.md` | messages pack primitives | iMessage/WhatsApp contact metadata import/review | Message contacts artifacts; merge through `$import-network --include-existing-artifacts` |
| LinkedIn CSV inside `$import-network` | `packs/ingestion/skills/import-network/SKILL.md` or `import-linkedin-network` | `packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py` called by orchestrator | LinkedIn `Connections.csv`, enrichment cache/API approval | LinkedIn `people.csv`; merged by orchestrator |

## Why skills are not the runtime handler

A skill is the harness-facing contract: when the user says `$import-email`, the
agent loads `SKILL.md` and follows its instructions. The runtime handler is the
Python primitive/orchestrator because it needs deterministic CLI arguments,
ledger/resume state, JSON output, tests, and child approval gates.

So the flow is:

```text
user command → skill routing/instructions → primitive/orchestrator → artifacts
```

For end-to-end tests prefer `$import-network` / `import_network_pipeline.py`; it
proves the source primitive, merge contracts, `network_companies.csv`, and DuckDB
loader still work together.
