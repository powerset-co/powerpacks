# Powerpacks 0.2.0 release draft

## Headline

Powerpacks 0.2.0 is the first end-to-end local network-search pipeline: import local relationship sources, enrich LinkedIn profiles, build local search artifacts, materialize DuckDB, and search people/companies through local DuckDB-backed agent/CLI flows. The local console covers setup, index status, DuckDB build actions, and result review.

## Data pipeline functional now

### Linking / local source setup

- Gmail/msgvault: local email/contact metadata import path
- LinkedIn: Connections CSV upload/import path
- Messages/WhatsApp: iMessage + WhatsApp local contact metadata import paths

### Discovery / import

- Gmail: `gmail_network_import.py msgvault` imports msgvault email metadata into local network artifacts.
- LinkedIn: `linkedin_network_import.py` converts Connections CSV into the shared people schema.
- Messages/WhatsApp: messages primitives produce contact artifacts that can be merged into the local network.

### Enrichment / identity resolution

- RapidAPI LinkedIn: hydrates LinkedIn-identified rows with profile data, work history, education, location, headline, summary, profile photo, skills, and social counts when returned.
- Cache-first profile enrichment: local RapidAPI cache hits complete without provider calls; cache misses are approval-gated.
- Gmail LinkedIn resolution: queues unresolved email/name/company candidates; Parallel-based resolution is spend-gated.
- OpenAI: role enrichment, company sector/entity classification, age inference, and embeddings. The indexing artifacts are local, but full processing can make OpenAI calls.

### Merge / indexing / materialization

- Merges source people into `.powerpacks/network-import/merged/people.csv` with merge confidence, source channels, and review flags.
- Builds `network_contacts.csv`, `network_contact_sources.csv`, and `network_companies.csv` for local attribution/navigation.
- Flattens people into position-level records.
- Enriches/dedupes roles with role IDs, seniority, track, doc2query, dense text.
- Classifies companies into entity/sector/semantic text.
- Builds people, companies, summaries, education, schools, and location records.
- Embeds roles, companies, and summaries with `text-embedding-3-small`.
- Materializes `.powerpacks/search-index/local-search.duckdb` for local search.

## Sources / providers

- Local files: LinkedIn Connections CSV, msgvault Gmail export, iMessage metadata, WhatsApp metadata, merged Powerpacks CSVs.
- RapidAPI: LinkedIn profile enrichment; optional Twitter/X follower crawl and LinkedIn validation.
- Parallel.ai: optional paid LinkedIn resolution / deep research for review queues.
- OpenAI: role/company/age/embedding processing.
- DuckDB: local search backend; no Supabase/Postgres/TurboPuffer upload for local indexes.

## Local Search functional now

- People retrieval: role/title semantic + BM25 search, role IDs/tracks, seniority, company constraints, current/past scope, tenure/date windows, location, years of experience, education prefilters, inferred age, and social metric filters when those counts exist.
- People records / hydration: identity, LinkedIn URL, profile/headline/summary/photo/location, work history, company context, education, contact/source metadata, and conditional X/Twitter, LinkedIn, and Instagram handles/counts from provider/import payloads.
- Company / semantic search: exact/alias company resolution, semantic company queries over name/description/sector/entity/doc2query text, company-domain adjacency, company-to-people handoff, and geography when present.

## Missing / not yet reliable

- Dedicated company base data is not local yet: headcount, funding total, founded year, valuation, and last funding date usually default to empty/zero.
- Investor-backed company filters are not reliable locally: `investor_urns` is empty without a dedicated company provider.
- Funding-stage / funding-total filters are schema-supported but usually missing local data.
- Separate company-signals namespace is not implemented.
- X/Twitter is not available for every person; it is optional/import-dependent, not part of the default LinkedIn-only path.
- LinkedIn follower/connection counts are not guaranteed; they depend on provider fields returned by RapidAPI/cache.
- No broad Sales Nav import into the local DuckDB search index in this release.
- No Supabase/Postgres/TurboPuffer write path for local indexes; local release uses DuckDB.
