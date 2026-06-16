# Changelog

## [0.7.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.6.0...powerpacks-v0.7.0) (2026-06-16)


### Features

* button copy 'Process Contacts' with count on its own line (LinkedIn + Gmail) ([ba33487](https://github.com/powerset-co/powerpacks/commit/ba334870bc0b6f3e4d14b6425a0fa9a976e7643c))
* drop email step from /onboarding, move full wizard to /onboarding-v2 ([6ef5434](https://github.com/powerset-co/powerpacks/commit/6ef5434b14c7cf15a476ba3e0bfa889c181d82bc))
* drop email step from /onboarding, move full wizard to /onboarding-v2 ([f2d9f16](https://github.com/powerset-co/powerpacks/commit/f2d9f16b749cf5c141533b2882dd098fc63e54b9))
* incremental Parallel.ai cost estimate under Gmail Process button ([3102b97](https://github.com/powerset-co/powerpacks/commit/3102b97e574cddf91f8b524854910f05fd8493ed))
* show incremental Parallel.ai cost estimate under Gmail Process button ([11b87dc](https://github.com/powerset-co/powerpacks/commit/11b87dcd7efdd16e1a9358e1fdf21245c39dd48f))
* single "Process" button for Gmail (local enrich -&gt; Modal index) ([f482423](https://github.com/powerset-co/powerpacks/commit/f48242394ec8b1e099d0fcaea2d4538462286733))
* single Process button for Gmail (local enrich → Modal index) ([ebff403](https://github.com/powerset-co/powerpacks/commit/ebff403ac59984d8a60a6bee39b4dcf9f8f35c85))


### Bug Fixes

* don't treat stale (killed) Modal runs as in-progress ([318ea30](https://github.com/powerset-co/powerpacks/commit/318ea30ccf75f05804f3d49f98bbdb59f2ebaa11))
* don't treat stale (killed) Modal runs as in-progress ([ba40f44](https://github.com/powerset-co/powerpacks/commit/ba40f44a8ad2bc19af684f2ebd5ac5eafe0704a1))

## [0.6.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.5.0...powerpacks-v0.6.0) (2026-06-16)


### Features

* collapse BYO keys behind a chevron; open Codex via codex:// deeplink ([22d1ef5](https://github.com/powerset-co/powerpacks/commit/22d1ef5bb8fe4d858e86a53d3057813516bbaacf))
* double-line stepper (label stacked under each circle) ([4dab26c](https://github.com/powerset-co/powerpacks/commit/4dab26c8ed682d8c3722349eef046ec28ccdaa1b))
* GCP-free Modal setup — pull runtime keys from Powerset API ([2710c51](https://github.com/powerset-co/powerpacks/commit/2710c517b73ac1c9d3c2bf7cc1b0f5b975b19538))
* Gmail sync date-window + per-vertical source pages ([#67](https://github.com/powerset-co/powerpacks/issues/67)) ([a3f4176](https://github.com/powerset-co/powerpacks/commit/a3f4176641c1e793c2d3d151420e1dd53f585ce1))
* Gmail vault setup UI + onboarding consolidation + bin/launch ([#71](https://github.com/powerset-co/powerpacks/issues/71)) ([f6f4041](https://github.com/powerset-co/powerpacks/commit/f6f4041fb8094018e74ed166386c45facf380ddc))
* hydrate uncached companies via RapidAPI by id and slug during indexing ([e28eb75](https://github.com/powerset-co/powerpacks/commit/e28eb75cf1e4ee1a2126e24aaae2c06720ea4ef6))
* make deep-dive a standard step + fix limit-200 default in search-profile ([#70](https://github.com/powerset-co/powerpacks/issues/70)) ([e5c764f](https://github.com/powerset-co/powerpacks/commit/e5c764f5bd8ad3b3de14866b56831e0a4e457413))
* onboarding-v3 pulls runtime keys after Powerset login with progress ([8d523ff](https://github.com/powerset-co/powerpacks/commit/8d523ff540a8e7c231043644ddc113274a51b36a))
* onboarding-v3 wizard — Powerset login / BYO keys / first search ([b84e100](https://github.com/powerset-co/powerpacks/commit/b84e100b4605b6068b4b0fe8aeeb72991ab50f16))
* onboarding-v3 wizard (Powerset login / BYO keys / first search) ([59b5bfb](https://github.com/powerset-co/powerpacks/commit/59b5bfb07daef0fc481ed8d1b259045d5183afdb))
* per-vertical source pages with link-only + load-time auto-discover ([#68](https://github.com/powerset-co/powerpacks/issues/68)) ([b494869](https://github.com/powerset-co/powerpacks/commit/b494869207d6543c01474042f07141aa533c62fd))
* prefill Codex deeplink + mark import step complete in stepper ([837a726](https://github.com/powerset-co/powerpacks/commit/837a7267d79789cd425bc634eae7e455334bdbce))
* pull_runtime_keys — fetch Modal token + OpenAI key from API, no GCP ([fc4c92e](https://github.com/powerset-co/powerpacks/commit/fc4c92e68e5e96548cbe4c6992d78fcc6a926d36))
* quorum-based JD candidate scoring with bar-raiser verdict ladder ([#64](https://github.com/powerset-co/powerpacks/issues/64)) ([5e84662](https://github.com/powerset-co/powerpacks/commit/5e846620eb2ed29873b872fbdfde8bb0323718f6))
* reuse company classification by LinkedIn slug + skip unresolved companies ([093d69c](https://github.com/powerset-co/powerpacks/commit/093d69c9a283a541aa45ae2b2c5ee5e99e44b5be))
* reuse company classification by LinkedIn slug and skip unresolved companies ([ef7b66c](https://github.com/powerset-co/powerpacks/commit/ef7b66c114cc81484b454e0528a6d9490a64c431))
* route env pull to pull_runtime_keys; drop gcloud from doctor ([d43257a](https://github.com/powerset-co/powerpacks/commit/d43257adbef2a72c70305c52cac55efd8904b983))
* single "Process" button on LinkedIn source page runs Modal enrich+index ([b8eedea](https://github.com/powerset-co/powerpacks/commit/b8eedea8f9b0adb5542f39c2ea76c9f1d6ccaad0))
* single Codex launch button on first-search step ([24e1c60](https://github.com/powerset-co/powerpacks/commit/24e1c6027fe6306e5edd40127bd76634f22ef63b))


### Bug Fixes

* make local Gmail vault setup and per-account authorize/sync work end-to-end ([58bb39c](https://github.com/powerset-co/powerpacks/commit/58bb39c75fdd5a6d06aa723b9480ccffdf624d24))
* make msgvault setup survive a reserved project id and Google's automation block ([95db21b](https://github.com/powerset-co/powerpacks/commit/95db21b0a3d1419344cf8a5b3d182c4b02acd3b1))
* only PAID-hydrate corpus-missing companies; add RapidAPI key override ([ef03579](https://github.com/powerset-co/powerpacks/commit/ef03579334ab8fa5547b86d9cedc46e8ec8f128c))
* raise duckdb memory_limit to 12GB for full-network index builds ([fb0fdcb](https://github.com/powerset-co/powerpacks/commit/fb0fdcb9243aa8d63cb20edfb8e7a36234d62987))
* refresh shared caches before duckdb build so enrichment persists on failure ([ced6f0d](https://github.com/powerset-co/powerpacks/commit/ced6f0daeda410372be5aa6e989eb33604a14a9a))
* **search:** scope source/interaction provenance to in-set operators ([#73](https://github.com/powerset-co/powerpacks/issues/73)) ([2b87ec0](https://github.com/powerset-co/powerpacks/commit/2b87ec0e7a135a6c95d7f5d2cc49e0684a3dfe6b))


### Documentation

* strip gcloud narrative from powerset SKILL (lean Modal flow) ([3ba3112](https://github.com/powerset-co/powerpacks/commit/3ba3112ae57542d81cafc483cd30aefe6f1f156e))

## [0.5.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.4.0...powerpacks-v0.5.0) (2026-06-13)


### Features

* add schema validator primitive for agent-authored artifacts ([4367e32](https://github.com/powerset-co/powerpacks/commit/4367e3272aade9534ea6a74e2485fae5aa84de3e))
* batch role enrichment calls 100 titles per request (prod parity) ([5a07440](https://github.com/powerset-co/powerpacks/commit/5a07440da87cd46f895e1e4f9b796691ac491fdd))
* cache preload command and tuned sandbox OpenAI settings ([7dcc6f7](https://github.com/powerset-co/powerpacks/commit/7dcc6f7f37b47c8493be689f709fd389eb3cafc6))
* carry interaction counts end-to-end and add tier-0 identifier matching ([ada2b76](https://github.com/powerset-co/powerpacks/commit/ada2b766aacb0249d850c787b97a45f9a499fa4c))
* carry interaction counts end-to-end and add tier-0 identifier matching ([6bdba06](https://github.com/powerset-co/powerpacks/commit/6bdba0684f938f8ff66dd3bc790506c9e14cd4dd))
* clearer file-attached state and standalone count/estimate on onboarding-v3 ([e0ddf2a](https://github.com/powerset-co/powerpacks/commit/e0ddf2acea12c66e0481716edacc693813af2f97))
* clearer file-attached state and standalone count/estimate on onboarding-v3 ([ab1d722](https://github.com/powerset-co/powerpacks/commit/ab1d722986b286422f7b05485504816014a4179b))
* configurable OpenAI service tier, standard tier for Modal onboarding ([52a9f6d](https://github.com/powerset-co/powerpacks/commit/52a9f6d1ef190e19947e1b258bc6baa424a8c896))
* enforce JD seniority bands at retrieval in profile search ([d788ea1](https://github.com/powerset-co/powerpacks/commit/d788ea158299e6ff0ca59b1c016fe89c1f9c7ad7))
* infer seniority bands from the role title when a JD states no level ([0ad67c1](https://github.com/powerset-co/powerpacks/commit/0ad67c132f96080772e33630b72de4b6815a2cfe))
* LinkedIn connections.csv to searchable index on Modal (onboarding v3) ([bf1a679](https://github.com/powerset-co/powerpacks/commit/bf1a6793e688c27f6bf8437d24b80f3b778c8be1))
* LinkedIn connections.csv to searchable index pipeline on Modal ([82a0bda](https://github.com/powerset-co/powerpacks/commit/82a0bdae537a0a55323739ba2b2a24f4860be0bb))
* make Modal cloud indexing work out of the box from env pull ([#54](https://github.com/powerset-co/powerpacks/issues/54)) ([305657a](https://github.com/powerset-co/powerpacks/commit/305657abb4283ec30a6f48f3407f20ef7a902143))
* onboarding-v3 console page for LinkedIn csv to cloud index ([2d3159b](https://github.com/powerset-co/powerpacks/commit/2d3159bcfdb0bf7f8e73faac61b926684a6414b8))
* persist setup job driver logs under .powerpacks ([2b26781](https://github.com/powerset-co/powerpacks/commit/2b26781079f700605b2618bd44c85db505f5f472))
* persist setup job driver logs under .powerpacks/runs/job-logs ([a32af20](https://github.com/powerset-co/powerpacks/commit/a32af201d83872529bf5cedc2a9951add654426c))
* stream paid enrichment results instead of gathering waves ([8e7838a](https://github.com/powerset-co/powerpacks/commit/8e7838a29a99a95cb6c7c45fad7785bbf78cb552))
* timestamp each line in setup job driver logs ([4acc82d](https://github.com/powerset-co/powerpacks/commit/4acc82d5a68bd51ccdec1c4cf5e16722aa7212ab))
* timestamp each line in setup job driver logs ([f847f20](https://github.com/powerset-co/powerpacks/commit/f847f20615ab92988ca66c5f2c4c5b1f1ffa650c))


### Bug Fixes

* build local_person_profiles in modal indexing so /contacts populates ([9cdc49b](https://github.com/powerset-co/powerpacks/commit/9cdc49b85d0b145335a44f768c62a5e6148d005a))
* build local_person_profiles in modal indexing so /contacts populates ([a8454d3](https://github.com/powerset-co/powerpacks/commit/a8454d377011bbeea27173ba218e63f7e00a1ff8))
* generate probe_summaries deterministically and share its reader contract ([1d54faa](https://github.com/powerset-co/powerpacks/commit/1d54faa601037d628fbb5e28fab16f838623944d))
* honor trait temporals at the local prepare boundary ([635ce3e](https://github.com/powerset-co/powerpacks/commit/635ce3e257254ca18bb1ff59266177f1cde93dd0))
* make search-profile plan preview a hard stop with re-confirmation ([507659b](https://github.com/powerset-co/powerpacks/commit/507659b20137d71435d26dbc8b30b9104e7c1ed2))
* matching never expands the user's approved contact set ([166e791](https://github.com/powerset-co/powerpacks/commit/166e791fda349a7bf977146f73a55a8a28208ae7))
* messages import diff tolerates an empty materialize result ([346882c](https://github.com/powerset-co/powerpacks/commit/346882c43a76bbe31869cccc8b3de7007e73bb59))
* messages import diff tolerates an empty materialize result ([bd7b5e5](https://github.com/powerset-co/powerpacks/commit/bd7b5e5ba72f9fe9c817304f2244e2f0be8a0456))
* messages import refreshes people.csv when approved contacts' counts change ([abf11a2](https://github.com/powerset-co/powerpacks/commit/abf11a2a7184406f1c0e473cb887bc0bdf7e3428))
* messages import self-invalidates when people.csv predates interaction columns ([76cdb1c](https://github.com/powerset-co/powerpacks/commit/76cdb1ce4ba0c6d2703cffcfbe2766586d9cadef))
* surface empty seniority bands in previews and ban YOE-derived bands everywhere ([d79275a](https://github.com/powerset-co/powerpacks/commit/d79275ae427875449d108610ded5925705599333))


### Performance Improvements

* parallel cache classification and skip estimate pass for internal runs ([bf204de](https://github.com/powerset-co/powerpacks/commit/bf204debd0cbcf53ab6cec39e46c7aecb6d77fce))


### Documentation

* record interaction-counts implementation and verification results ([3abd212](https://github.com/powerset-co/powerpacks/commit/3abd212f70c4bde06cb17efd093e6fcfe261d660))
* record matching approval-gate rule and verification ([4e6067f](https://github.com/powerset-co/powerpacks/commit/4e6067f6db5a3f47f42f8b5c63cf6f9301b984c9))

## [0.4.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.3.1...powerpacks-v0.4.0) (2026-06-12)


### Features

* add agentic SQL search vertical over local DuckDB ([9fb23d2](https://github.com/powerset-co/powerpacks/commit/9fb23d2b76700d43aedadb5db78a3c1b1bb1f532))
* add agentic SQL search vertical over local DuckDB ([76c95ea](https://github.com/powerset-co/powerpacks/commit/76c95eaa52d0a3316b43db648d187e26cf69b6a5))
* add contacts, profiles, company directory, and local search to console ([091722d](https://github.com/powerset-co/powerpacks/commit/091722dc762f53dbbab55742e73f28057e3a507b))
* add cross-trait trigger to the agentic SQL fan-out gate ([a6cb0a2](https://github.com/powerset-co/powerpacks/commit/a6cb0a2ef4c80f728787127927e78d4386f8064e))
* add hiring seniority and recruitability defaults to search skills ([364a84d](https://github.com/powerset-co/powerpacks/commit/364a84d98f50dea70be894425b2b6aa221333c5f))
* add launchd daemon mode to the console run script ([d299848](https://github.com/powerset-co/powerpacks/commit/d299848b0498fa3f49106998cc963a3234bfb1d9))
* add Modal indexing PoC and stream pipeline memory hot paths ([#51](https://github.com/powerset-co/powerpacks/issues/51)) ([80ccc85](https://github.com/powerset-co/powerpacks/commit/80ccc858337749c1e7def5930166c94357ec95d0))
* add person-lookup fast path, zero-result SQL fallback, and pool-size preview gate ([58206d2](https://github.com/powerset-co/powerpacks/commit/58206d27c1310ab0aba504e8f17f40af97cf2fe8))
* backfill company HQ locations from rapidapi cache in indexing pipeline ([377ded4](https://github.com/powerset-co/powerpacks/commit/377ded4811896bae548bd5abdbe059239a9790ee))
* fan agentic SQL candidates into the shared rerank pipeline ([a9c61a9](https://github.com/powerset-co/powerpacks/commit/a9c61a9b6a1ef7e5765a12526a02846030df8e28))
* gate agentic SQL fan-out behind a crisp relational-query rubric ([38179ab](https://github.com/powerset-co/powerpacks/commit/38179ab6c26bdef9175b33eae92aea08e1f84f32))
* grade skill evals with an LLM judge instead of keyword matching ([6534502](https://github.com/powerset-co/powerpacks/commit/6534502107547a5b238b0e5cd9022fbc60add34d))


### Bug Fixes

* cap company semantic lookup top_k at 1000 per subquery ([571425e](https://github.com/powerset-co/powerpacks/commit/571425ee91ddabc02bfe03fb0a651d6cb1316430))
* configure local backend mode before prepare/run payload transforms ([1080d39](https://github.com/powerset-co/powerpacks/commit/1080d39a0d95bcde8b3ff208585ae439dd9c75db))
* cut resolve_companies latency from minutes to seconds on large pools ([4127c36](https://github.com/powerset-co/powerpacks/commit/4127c368686129151047463d53691bd2c5bcceae))
* fork per-call DuckDB cursors instead of sharing one connection across threads ([aa9fedb](https://github.com/powerset-co/powerpacks/commit/aa9fedbb59b468c3a673f5611784b0713f6908ae))
* local prefilter fails with 'missing table' under chunked company fan-out ([d1b2339](https://github.com/powerset-co/powerpacks/commit/d1b23394d7b7aeeeffe2f074afdf7136827638d8))
* local prepare/run built filters in remote mode, zeroing pools under a foreign set id ([8b45ec5](https://github.com/powerset-co/powerpacks/commit/8b45ec5032e679e0009a06aad4892cccd742e5f6))
* strip set/operator scope keys from local payloads outright ([f91e09b](https://github.com/powerset-co/powerpacks/commit/f91e09b800f3d1fd1aa703f4951e5d9983e901f7))

## [0.3.1](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.3.0...powerpacks-v0.3.1) (2026-06-11)


### Bug Fixes

* align extraction value spaces with canonical index taxonomies ([b4f474b](https://github.com/powerset-co/powerpacks/commit/b4f474b67cf47a069be5a135215d359a70650c82))
* align local extraction with index taxonomies and deployed prod retrieval semantics ([dd310bc](https://github.com/powerset-co/powerpacks/commit/dd310bc01b0790441d4a45af6b0559d6dbac4b77))
* keep local parity execution fully local ([b4ccb00](https://github.com/powerset-co/powerpacks/commit/b4ccb003ade598a5ac673320f1ca6dc390c3b7f6))
* reserve hard role_ids filters for query-named shortcut roles ([25264f0](https://github.com/powerset-co/powerpacks/commit/25264f03507c2be47ee775f67a94686a954d92f0))

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
