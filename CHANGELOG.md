# Changelog

## [0.3.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.2.0...powerpacks-v0.3.0) (2026-06-10)


### Features

* add repo-local pipeline reuse, incremental DuckDB indexing, and LinkedIn onboarding v2 ([#30](https://github.com/powerset-co/powerpacks/issues/30)) ([eae042d](https://github.com/powerset-co/powerpacks/commit/eae042d8c81efe68828e93585b1fdec95157b16f))
* derive metro areas from prod city-to-metro mapping ([419a63a](https://github.com/powerset-co/powerpacks/commit/419a63a217e4e3948b11af91aad6da332be1f81b))
* find similar people from a LinkedIn URL ([#45](https://github.com/powerset-co/powerpacks/issues/45)) ([07cbca2](https://github.com/powerset-co/powerpacks/commit/07cbca2150c8bab3a658d46bb61edb625a9dc755))
* join company enrichment onto people positions in step_people ([56e5e74](https://github.com/powerset-co/powerpacks/commit/56e5e74eca10bae018a2623ef9045123a508aa6a))
* local LLM rerank, alias-union merge, and msgvault RFC822 dedupe ([#43](https://github.com/powerset-co/powerpacks/issues/43)) ([a834f81](https://github.com/powerset-co/powerpacks/commit/a834f81470a5ba9b9b3ff842b0d8514cea5b835a))
* local pipeline prod parity — contract-driven schemas, record completeness, search parity ([e03987d](https://github.com/powerset-co/powerpacks/commit/e03987d541147f694510ec4477e7eae0f467e659))
* Messages onboarding v2, accounts.json writeback, spend estimate fix ([a2d892e](https://github.com/powerset-co/powerpacks/commit/a2d892e00755218128dfaa8e8346b75fb2359a07))
* mirror prod local search execution pools ([#37](https://github.com/powerset-co/powerpacks/issues/37)) ([dbac404](https://github.com/powerset-co/powerpacks/commit/dbac4047f0417f784833ec73573a34808a18ed4a))
* search-profile skill — recruiter profiles, budgeted searches, automated seniority-gated evaluation ([#39](https://github.com/powerset-co/powerpacks/issues/39)) ([ce38c63](https://github.com/powerset-co/powerpacks/commit/ce38c638377786ea33fb32c04bea966a33584493))
* split JD search and improve search reranking ([#36](https://github.com/powerset-co/powerpacks/issues/36)) ([0ae2fd4](https://github.com/powerset-co/powerpacks/commit/0ae2fd49defbfc9eca5791ed6a346e771d0dfcc8))
* widen namespace contracts and make them the single schema source ([497a5ce](https://github.com/powerset-co/powerpacks/commit/497a5ced8cd3f5477e06ce89110f6333e9d714b0))


### Bug Fixes

* align msgvault Gmail interaction counting ([#35](https://github.com/powerset-co/powerpacks/issues/35)) ([85434cc](https://github.com/powerset-co/powerpacks/commit/85434ccf53acd7128a7b4e389b4f6adecdcdc83d))
* complete education, summaries, and profile record builders ([d683ba7](https://github.com/powerset-co/powerpacks/commit/d683ba746f9ea38ff7d335e73ea738189848fe69))
* disambiguate Powerset-network vs local routing in search-network skill ([716bfbb](https://github.com/powerset-co/powerpacks/commit/716bfbbc9305b840ff6fd3c806369ba47912306b))
* local search parity with prod retrieval semantics ([f594118](https://github.com/powerset-co/powerpacks/commit/f5941189b70b600717618613d0c8f4ac75a56d4b))
* make Gmail discovery recount idempotent ([#38](https://github.com/powerset-co/powerpacks/issues/38)) ([17942bf](https://github.com/powerset-co/powerpacks/commit/17942bffb9fcb6bd3745c5d9274d9694ee7c63e2))
* persist RapidAPI company context onto records on all paths ([465ada6](https://github.com/powerset-co/powerpacks/commit/465ada6f4aa04555935916fdd6f7f9e742192c8f))
* show one compact seniority-target line in search previews ([633a330](https://github.com/powerset-co/powerpacks/commit/633a330c763f254107f08971b1ed7c5d7dad8466))
* stop echoing seniority policy in search-profile plan previews ([f8cc4fa](https://github.com/powerset-co/powerpacks/commit/f8cc4fa8d3ac6cb33cd53d80c863675fb7ff6516))


### Documentation

* add data pipeline simplicity guardrail to AGENTS.md ([#40](https://github.com/powerset-co/powerpacks/issues/40)) ([d3f0f0c](https://github.com/powerset-co/powerpacks/commit/d3f0f0c5b0382d56b05f7b3f9f35b9ab7e0f5f07))
* make PR tooling guidance conditional on Vorflux availability ([#42](https://github.com/powerset-co/powerpacks/issues/42)) ([781cb25](https://github.com/powerset-co/powerpacks/commit/781cb2573a41371bbb3d459c4a8a710571ef131f))
* track search-quality known issues from Jun 3-9 feedback ([c91c27f](https://github.com/powerset-co/powerpacks/commit/c91c27f5a9bae09594fe38c13675bb4754f37169))

## [0.2.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.1.0...powerpacks-v0.2.0) (2026-06-08)

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
