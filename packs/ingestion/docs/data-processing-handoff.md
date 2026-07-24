<!--
Changelog:
- 2026-07-23: Gmail discovery account selection is `--account-email` (repeatable)
  only; the `--accounts`/accounts-file alternative and the `discover()` wrapper
  were dropped from the primitive (callers construct `GmailDiscovery(...).run()`).
  Fixed the `$import-gmail` runtime-row path `gmail.py discover` →
  `gmail/discover.py discover --account-email ...`.
- 2026-07-23 (audit): linkedin/network_import.py + enrich/enrich_people.py migrated
  to manifest-only; both are now a single idempotent `run` writing one
  manifest.json (the per-primitive ledger runners, the `continue`/`approve`
  subcommands, and the delegate enrich_people.ledger.json are gone). The LinkedIn
  CSV path + the two LinkedIn/shared-enrichment RapidAPI gate rows now say spend is
  gated by `--approve-spend` (a needs_approval stop on cache misses); cache hits
  stay local-only. run_linkedin.py invokes the single approved `run` on Modal.
- 2026-07-23 (audit): twitter/network_import.py migrated to manifest-only; the
  `$import-twitter` runtime row is now `run` with spend gated by `--approve-spend`
  (the `approve`/`continue` subcommands and the ledger are gone), and the two
  Twitter RapidAPI gate rows note the artifact-cache behavior.
- 2026-07-23 (audit): gmail/msgvault_store.py split into the gmail/msgvault/
  package (store.py + util.py); gmail/sync.py moved to gmail/msgvault/sync.py.
- 2026-07-23 (audit batch 17): gmail/network_import.py rows retargeted to its
  split successors gmail/msgvault_store.py + gmail/discover_engine.py.
- 2026-07-23 (audit batch 16): removed the retired $discover-contacts skill and
  the deleted discover.py orchestrator from the tables; the
  merge phase is owned by the indexing fan-in.
- 2026-07-23: LinkedIn CSV path row now named the surviving surfaces after the
  wrapper-skill cleanup.
-->

# Data processing handoff: ingestion outputs, schemas, and enrichment boundary

> **Status: historical handoff.** Some skill names and orchestration details below
> predate `$import-gmail` and `$import-messages`. Start with the
> [ingestion docs index](README.md), [Gmail import pipeline](gmail-import-pipeline.md),
> and [Message import pipeline](message-import-pipeline.md).

This handoff is for the data-processing worker. It describes the artifacts now
emitted by local network ingestion and where RapidAPI enrichment happens.

## End-to-end phases

```txt
account linking / local source setup
  -> ingestion/source import
  -> enrichment / identity resolution
  -> merge
  -> processing / indexing / search
```

| Phase | Owner surface | Current scripts | Notes |
| --- | --- | --- | --- |
| Account linking / local source setup | source-specific onboarding | msgvault CLI, LinkedIn export UI, `$import-messages`, Twitter RapidAPI key checks | Establish source access; avoid provider/API work unless explicitly approved. |
| Ingestion/source import | ingestion skills + primitives | `gmail/discover_engine.py msgvault`, `linkedin/network_import.py`, `twitter/network_import.py`, messages primitives | Produces source-local normalized `people.csv` or message contacts artifacts. |
| Enrichment / resolution | data processing + source-specific gates | `enrich_people.py`, Twitter `pre_resolve_linkedin`/`validate_linkedin`, future resolver queues | RapidAPI LinkedIn profile enrichment is centralized in `enrich_people` for LinkedIn-identified rows; Twitter still has source-specific validation. |
| Merge | indexing fan-in | `merge_network_sources.py` (via `index_contacts_pipeline.py`) | Produces merged CSV contracts and contact/source provenance. |
| Processing / indexing / search | data processing / indexing/search packs | indexing primitives consume merged artifacts | Should consume canonical CSVs/views, not source-specific raw dumps. |

## User-facing skills and runtime handlers

Skills are harness-facing handlers/instructions. Runtime work is done by Python
primitives so runs are deterministic, ledgered, testable, and resumable.

| User command / skill | Skill file | Runtime script(s) | Result |
| --- | --- | --- | --- |
| `$import-gmail` | `packs/ingestion/skills/import-gmail/SKILL.md` | bounded `gmail/discover.py discover --account-email ...` -> directory/Parallel/RapidAPI import -> fan-in -> Modal index | msgvault Gmail metadata imported into the local network and search index. |
| `$import-twitter` | `packs/ingestion/skills/import-twitter/SKILL.md` | `twitter/network_import.py run` (spend gated by `--approve-spend`); then the indexing fan-in | Twitter/X `people.csv`, then merged local network artifacts. |
| `$import-messages` | `packs/ingestion/skills/import-messages/SKILL.md` | ingestion discovery/match/research/review -> source import -> fan-in -> Modal index | Reviewed iMessage/WhatsApp metadata in merged local network artifacts. |
| LinkedIn CSV path | `$setup` (or `linkedin/network_import.py` directly) | `linkedin/network_import.py run` (spend gated by `--approve-spend`) delegating to `enrich_people.py` | LinkedIn Connections export plus shared RapidAPI/cached profile enrichment. |

## Canonical CSV contracts

### `people.csv`

Canonical provider-neutral local person interchange artifact. Columns come from
`packs/ingestion/schemas/people_schema.py::PEOPLE_SCHEMA_COLUMNS`.

```txt
id, public_identifier, linkedin_url, first_name, last_name, full_name,
headline, summary, city, state, country, location_raw, profile_picture_url,
work_experiences, education, current_title, current_company,
current_company_urn, entity_urn, enrichment_provider, enriched_at,
harmonic_response, harmonic_location, rapidapi_response, twitter_handle,
twitter_response, primary_email, all_emails, primary_phone, all_phones,
source_channels, source_artifacts
```

Merge adds these columns to merged `people.csv`:

```txt
merge_key, merge_confidence, merge_sources, merged_row_count, needs_review
```

### `network_contacts.csv`

One row per merged local contact/network entity.

```txt
contact_id, merge_key, display_name, linkedin_url, public_identifier,
primary_email, primary_phone, source_channels, source_count, needs_review
```

### `network_contact_sources.csv`

Source/provenance facts for contacts. This mirrors the local equivalent of an
`operator_person_sources` style table and should be used for UI attribution.

```txt
contact_id, merge_key, source_channel, source_identifier, source_artifact,
display_name, linkedin_url, public_identifier, primary_email, primary_phone
```

Known `source_channel` values include:

```txt
linkedin_csv, gmail_msgvault, imessage, whatsapp, twitter
```

### `network_companies.csv`

Contact-derived company summary for local company search/navigation. This is not
a fully enriched company corpus; it is derived from current person company
fields, primarily `current_company` and `current_company_urn`.

```txt
company_id, company_key, company_name, company_urn, source_channels,
contact_count, contact_ids, contact_names
```

## Search materialization

The former separate contact lookup DuckDB was retired because nothing consumed
it. The merged CSVs remain the source/provenance contract. Search, contact lookup,
and local profile inspection use
`.powerpacks/search-index/local-search.duckdb`, built by the indexing pipeline.

## RapidAPI enrichment boundary

Current RapidAPI behavior is per vertical/source, with a shared LinkedIn profile
enrichment primitive for rows that already have LinkedIn identity.

| Surface | RapidAPI usage | Where it happens | Gate |
| --- | --- | --- | --- |
| LinkedIn CSV import | LinkedIn profile enrichment for cache misses | `linkedin/network_import.py` delegates to `enrich_people.py` | Gated by `--approve-spend` (a `needs_approval` stop on cache misses, forwarded to the delegate); cache hits are local-only. |
| Shared people enrichment | LinkedIn profile enrichment for rows with `linkedin_url` / `public_identifier` | `enrich_people.py` | Gated by `--approve-spend` (a `needs_approval` stop on RapidAPI cache misses); cache hits never spend. |
| Twitter/X import | Twitter follower crawl | `twitter/network_import.py` step `load_or_crawl` | Gated by `--approve-spend`; cached once `followers_dump.csv` exists. |
| Twitter/X import | LinkedIn validation/enrichment for pre-resolved LinkedIn URLs | `twitter/network_import.py` step `validate_linkedin` | Gated by `--approve-spend`; cached once `linkedin_validated.csv` exists. |
| Gmail/msgvault | No lookups inside the import; `gmail/discover_engine.py msgvault` emits `linkedin_resolution_queue.csv` and `gmail/discover_engine.py apply-resolutions` attaches already-STORED resolutions | new LinkedIn resolution/research is owned by `$deep-context` (`deep_context/deep_research_contacts.py` + the identity judge) | msgvault import local-only; all resolution spend lives behind deep-context gates; RapidAPI only later through `enrich_people.py`. |
| Messages/iMessage/WhatsApp | None in local import today | ingestion message primitives | Local metadata/review only unless later resolver/enrichment is explicitly added. |

Target direction: source verticals emit canonical `people.csv` or resolution
queues; LinkedIn URL resolution/research is owned by `$deep-context`
(`deep_context/deep_research_contacts.py` + the identity judge), and
`enrich_people.py` owns LinkedIn profile hydration — no vertical owns its own
enrichment implementation.

## Regression checks

```bash
uv run --project . python -m unittest \
  tests/test_discover.py \
  tests/test_merge_network_sources.py \
  tests/test_enrich_people.py \
  tests/test_linkedin_import.py \
  tests/test_twitter_import.py \
  tests/test_gmail_import.py
```
