# Changelog

## [1.2.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v1.1.0...powerpacks-v1.2.0) (2026-07-24)


### Features

* **discover/gmail:** rebuild contacts.csv when the output is empty or a full rerun is asked for ([#334](https://github.com/powerset-co/powerpacks/issues/334)) ([078819c](https://github.com/powerset-co/powerpacks/commit/078819c246928646533b4f5e330c4d678ff8f651))
* **enrich:** annotate enrichment failures instead of deleting rows, and cache only permanent failures ([#331](https://github.com/powerset-co/powerpacks/issues/331)) ([71bb20f](https://github.com/powerset-co/powerpacks/commit/71bb20f0bd13c69fd60b5a5a7d205cf31fb1a165))
* **ingestion:** a person with a LinkedIn key, an email, or a phone is a person ([#330](https://github.com/powerset-co/powerpacks/issues/330)) ([48852df](https://github.com/powerset-co/powerpacks/commit/48852dff8a2128469427c1e0d0d606def080f294))


### Bug Fixes

* **ingestion:** one LinkedIn slug normalizer, and a merge key that re-derives it ([#329](https://github.com/powerset-co/powerpacks/issues/329)) ([7b6a7d2](https://github.com/powerset-co/powerpacks/commit/7b6a7d2be699142ee22950966cf7cbc3e46b8110))
* **install:** the release's own updater finishes the update ([#327](https://github.com/powerset-co/powerpacks/issues/327)) ([dbdb02f](https://github.com/powerset-co/powerpacks/commit/dbdb02f6c6b2484cc154cfb4d4398561fdef2737))

## [1.1.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v1.0.0...powerpacks-v1.1.0) (2026-07-24)


### Features

* **install:** installs follow published releases, not the tip of main ([#324](https://github.com/powerset-co/powerpacks/issues/324)) ([c6d39bb](https://github.com/powerset-co/powerpacks/commit/c6d39bb672c7eed79bbb71ab2a141010d163c9dd))

## [1.0.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.18.0...powerpacks-v1.0.0) (2026-07-24)


### ⚠ BREAKING CHANGES

* **ingestion:** ingestion primitive paths and CLI contracts changed. The discover_contacts_pipeline and import_contacts_pipeline packages are now discover and imports; gmail's discover engine is extract_gmail and the WhatsApp binary client split out of the extractor; account selection is explicit CLI flags with no accounts.json fallback; ledger subcommands (approve, continue, status) are replaced by a single idempotent run plus an approve-spend flag; and the retired skills, console app, and legacy resolver are removed.
* **deep-context:** import_contacts_pipeline/gmail.py no longer accepts --resolve-legacy / --approve-parallel-spend; in-import Parallel resolution and RapidAPI hydration are permanently removed in favor of bin/deep-context migrate-legacy + the judged review loop.
* removes retired primitives (llm_review_contacts, prepare_research_queue, build_research_review_csv, review_research_web, estimate_gmail_sync, gmail_metadata_sync) and the before_split module.
* **deep-context:** the review app runs the mid-flow pipeline itself ([#258](https://github.com/powerset-co/powerpacks/issues/258))
* **deep-context:** replace the Codex bridge with review-status --wait ([#256](https://github.com/powerset-co/powerpacks/issues/256))
* **search:** packs/search/primitives/local_search_pipeline/ and packs/search/primitives/local_duckdb/ are removed; local runs use search_network_pipeline.py --backend local.
* **search:** packs/search/primitives/route_query/ is removed; the $search skill contract changed from classifier-dispatch to the recorded decision contract.
* **search:** $recruit and $search-profile are removed; use $search (a job URL / pasted JD / role brief routes to its deep mode automatically).

### Features

* **adapters:** auto-generated install stamp for stale-install detection ([#187](https://github.com/powerset-co/powerpacks/issues/187)) ([023cca2](https://github.com/powerset-co/powerpacks/commit/023cca252c71b5a9cdc3aeec9cda5f7497e9846d))
* **deep-context:** adaptive Gmail collection (1600 per vertical) ([#160](https://github.com/powerset-co/powerpacks/issues/160)) ([19a536e](https://github.com/powerset-co/powerpacks/commit/19a536ef031ddf84a8c46c27e331c3f8ea827e20))
* **deep-context:** adopt pre-sig merge verdicts by name so old files reuse for free ([#224](https://github.com/powerset-co/powerpacks/issues/224)) ([409a620](https://github.com/powerset-co/powerpacks/commit/409a6206763902ded537ea82f39a60552bec1208))
* **deep-context:** cache same-person verdicts so reruns only judge new pairs ([#223](https://github.com/powerset-co/powerpacks/issues/223)) ([52e5b01](https://github.com/powerset-co/powerpacks/commit/52e5b01400098c5bcf1262021e4ec6d0d333e3a2))
* **deep-context:** clearer, calmer merged-person review UI ([#156](https://github.com/powerset-co/powerpacks/issues/156)) ([475cb51](https://github.com/powerset-co/powerpacks/commit/475cb51dc1d9668539cc725ab20e0e66a6a15e97))
* **deep-context:** composite first/last-initial blocking key for same-person clustering ([#286](https://github.com/powerset-co/powerpacks/issues/286)) ([f6972ad](https://github.com/powerset-co/powerpacks/commit/f6972ad5f4a6620c9ff6ccd755687d36b380be06))
* **deep-context:** confident judge rejections stand without a human check ([#307](https://github.com/powerset-co/powerpacks/issues/307)) ([6c3b703](https://github.com/powerset-co/powerpacks/commit/6c3b703f39c4fc89cfef48e244c34275c9c72afd))
* **deep-context:** exclude ("X out") people from the network + live review-UI feedback ([#154](https://github.com/powerset-co/powerpacks/issues/154)) ([963795f](https://github.com/powerset-co/powerpacks/commit/963795fe635731927ed1da8a5ff20a24ee7b3e75))
* **deep-context:** human-readable plan copy + batch size 500 + review-UI polish ([#218](https://github.com/powerset-co/powerpacks/issues/218)) ([04c6bf7](https://github.com/powerset-co/powerpacks/commit/04c6bf7ea3941d28b698bc2f2e56377b6a544a4f))
* **deep-context:** judge-accepted found profiles stand without a human check ([#306](https://github.com/powerset-co/powerpacks/issues/306)) ([4b8c424](https://github.com/powerset-co/powerpacks/commit/4b8c424b8367a39c868e1c22107cd749b2606126))
* **deep-context:** live search across worth review views (table filter + maybe typeahead) ([#283](https://github.com/powerset-co/powerpacks/issues/283)) ([b843a87](https://github.com/powerset-co/powerpacks/commit/b843a87af67e4a756539b435d15a9115174f8b79))
* **deep-context:** LLM 2-sentence profile summaries in prefetch + card Summary precedence ([#233](https://github.com/powerset-co/powerpacks/issues/233)) ([3ac1707](https://github.com/powerset-co/powerpacks/commit/3ac17078362b83133bc7f97a3981bf8269c5d3f0))
* **deep-context:** lower the research confirm bar to 0.80 and share the predicate gate ([#308](https://github.com/powerset-co/powerpacks/issues/308)) ([58558dd](https://github.com/powerset-co/powerpacks/commit/58558ddbc401eba651e012ab72767f09505329e6))
* **deep-context:** merge same-person records linked by a message-only email ([#222](https://github.com/powerset-co/powerpacks/issues/222)) ([a4be3aa](https://github.com/powerset-co/powerpacks/commit/a4be3aac50e5aeb9c79f28364c7d902a4da005cd))
* **deep-context:** migrate legacy parallel resolutions into the reviewable retarget loop ([#316](https://github.com/powerset-co/powerpacks/issues/316)) ([3bc32b0](https://github.com/powerset-co/powerpacks/commit/3bc32b09192e99647576a30fb8a054b7033ea489))
* **deep-context:** name-match unlinked contacts to your LinkedIn connections ([#221](https://github.com/powerset-co/powerpacks/issues/221)) ([c5a50d9](https://github.com/powerset-co/powerpacks/commit/c5a50d9d931c7a36d4bf6250524e317cf11469ce))
* **deep-context:** profile-card review batch + Codex review-agent bridge ([#230](https://github.com/powerset-co/powerpacks/issues/230)) ([18dc651](https://github.com/powerset-co/powerpacks/commit/18dc651750a2decffee06dd26c9d692b6b90ccd1))
* **deep-context:** restart command + drop the prefetch nag from the review UI ([#249](https://github.com/powerset-co/powerpacks/issues/249)) ([7248b48](https://github.com/powerset-co/powerpacks/commit/7248b48a6cf7485a1876374e5f0069275bf94c31))
* **deep-context:** restart replays the FULL journey — clear human identity decisions too ([#251](https://github.com/powerset-co/powerpacks/issues/251)) ([a653f10](https://github.com/powerset-co/powerpacks/commit/a653f10f24d3dc85db1f4bb2cc524f31b3175f34))
* **deep-context:** retarget judge defaults to medium effort at 128 lanes ([#311](https://github.com/powerset-co/powerpacks/issues/311)) ([66fbeca](https://github.com/powerset-co/powerpacks/commit/66fbeca8371a1ee6e92d5c75dc9cb9f2a1de4f73))
* **deep-context:** review synthetic profiles in the UI + research plausibly-absent opt-in ([#189](https://github.com/powerset-co/powerpacks/issues/189)) ([dca613a](https://github.com/powerset-co/powerpacks/commit/dca613a50c7590a6803ea3ef92e7c7c789872863))
* **deep-context:** review UI overhaul + judged deep-research retargets ([#226](https://github.com/powerset-co/powerpacks/issues/226)) ([8ee87f0](https://github.com/powerset-co/powerpacks/commit/8ee87f0ae5a3505d8d0af65887f949d52a6b4168))
* **deep-context:** review-card layout fixes + expandable Add-People rows ([#217](https://github.com/powerset-co/powerpacks/issues/217)) ([b5c74aa](https://github.com/powerset-co/powerpacks/commit/b5c74aa6ec54be3e703060625a86095c3df22600))
* **deep-context:** review-only fast path for the skill ([#158](https://github.com/powerset-co/powerpacks/issues/158)) ([b6bf4df](https://github.com/powerset-co/powerpacks/commit/b6bf4dfc3d2cc48184825e99556427efdd966170))
* **deep-context:** shared identifiers decide merges in code, not model attention ([#313](https://github.com/powerset-co/powerpacks/issues/313)) ([2a27d40](https://github.com/powerset-co/powerpacks/commit/2a27d40e426b337e914954a1c2932fbbb6b08fe3))
* **deep-context:** spam-screen merge rule, Rejected tab, one-click merged-person review, re-review command ([#183](https://github.com/powerset-co/powerpacks/issues/183)) ([3dca6b8](https://github.com/powerset-co/powerpacks/commit/3dca6b8533fd0a707668e8de80a274b7a23de1fd))
* **deep-context:** synthetic profiles from deep research (no-LinkedIn people become searchable) ([#188](https://github.com/powerset-co/powerpacks/issues/188)) ([85af417](https://github.com/powerset-co/powerpacks/commit/85af417c83c745681abe43e842631613b7202b30))
* **deep-context:** the review app runs the mid-flow pipeline itself ([#258](https://github.com/powerset-co/powerpacks/issues/258)) ([78f721c](https://github.com/powerset-co/powerpacks/commit/78f721c96c44f35860fdbd994dd895b02396c734))
* **deep-context:** the staged flow only ever moves forward ([#309](https://github.com/powerset-co/powerpacks/issues/309)) ([e078c26](https://github.com/powerset-co/powerpacks/commit/e078c26ba332e28dbde35b296e674237810ccae6))
* **deep-context:** worth cards show the raw-message evidence with a stale callout ([#295](https://github.com/powerset-co/powerpacks/issues/295)) ([bb0fddb](https://github.com/powerset-co/powerpacks/commit/bb0fddb0c7b28062b8a931ad55feefb0a577a7f7))
* **deep-context:** worth_view.py — the entire worth stage in one file ([#272](https://github.com/powerset-co/powerpacks/issues/272)) ([403dd2f](https://github.com/powerset-co/powerpacks/commit/403dd2f281127948a16f8844e79630664eda96d2))
* **import-messages:** smart-default incremental WhatsApp sync + sync/full verbs ([#245](https://github.com/powerset-co/powerpacks/issues/245)) ([4f93906](https://github.com/powerset-co/powerpacks/commit/4f9390653ad186ff7510e905a0a420f94c98fdb0))
* **indexing:** parallelize processing stages ([ff50179](https://github.com/powerset-co/powerpacks/commit/ff501794875f8c8f43d922aae444b941d8ab489f))
* **indexing:** use Parquet embedding caches ([#201](https://github.com/powerset-co/powerpacks/issues/201)) ([1c83980](https://github.com/powerset-co/powerpacks/commit/1c83980e6dedf4ebaa184db9d095911178c5a9c7))
* **indexing:** use Parquet throughout local indexing ([06d6175](https://github.com/powerset-co/powerpacks/commit/06d6175b010b5319c4eae982a8c43384f2b8f54f))
* **ingestion:** $clean-slate — standalone skill for the full pipeclean ([#274](https://github.com/powerset-co/powerpacks/issues/274)) ([fa9672e](https://github.com/powerset-co/powerpacks/commit/fa9672e44206028b0148849124c82f1c2b1705f9))
* **ingestion:** $deep-setup — centralized post-import processing layer ([#206](https://github.com/powerset-co/powerpacks/issues/206)) ([df6a00c](https://github.com/powerset-co/powerpacks/commit/df6a00cabf1ec0cb733548a7c9da02f2e31bb29d))
* **ingestion:** bin/clean-slate — scrub derived state, preserve paid artifacts ([#270](https://github.com/powerset-co/powerpacks/issues/270)) ([703e778](https://github.com/powerset-co/powerpacks/commit/703e7787d650ef2e0cf30dd8133c0566259a098c))
* **ingestion:** context-informed network-worth triage (yes/maybe/no) + review filters ([#207](https://github.com/powerset-co/powerpacks/issues/207)) ([a7a4e60](https://github.com/powerset-co/powerpacks/commit/a7a4e605f5d98aa4d842684e0fe4bf557626fcb9))
* **ingestion:** deliver pinned wacli as a prebuilt download + refresh via $update-powerpacks ([#263](https://github.com/powerset-co/powerpacks/issues/263)) ([7ba9c40](https://github.com/powerset-co/powerpacks/commit/7ba9c40df04c1e57b9008fb558b2a8bac4a743cb))
* **ingestion:** detect pre-full-sync WhatsApp links and nudge a re-link ([#268](https://github.com/powerset-co/powerpacks/issues/268)) ([83f8df5](https://github.com/powerset-co/powerpacks/commit/83f8df5b1d0290b865108e08e861bd2647f00c6c))
* **ingestion:** imports focus on contact sync; candidates pool for deep-setup ([#205](https://github.com/powerset-co/powerpacks/issues/205)) ([b179208](https://github.com/powerset-co/powerpacks/commit/b1792089b8d9ebe36ebaaca00315f7c88c16dabb))
* **ingestion:** pin wacli to powerset-co full-sync fork + fix 0.13 QR ([#257](https://github.com/powerset-co/powerpacks/issues/257)) ([8745174](https://github.com/powerset-co/powerpacks/commit/87451746558a03c807885e222224c61dbd247415))
* **ingestion:** prompt to re-link pre-full-sync WhatsApp sessions ([#276](https://github.com/powerset-co/powerpacks/issues/276)) ([21a8b94](https://github.com/powerset-co/powerpacks/commit/21a8b945bd1da8e11635c27fa0b5a29bc1c9e911))
* **ingestion:** refresh pinned wacli when the pin bumps (version stamp) ([#260](https://github.com/powerset-co/powerpacks/issues/260)) ([60f0868](https://github.com/powerset-co/powerpacks/commit/60f0868558d18ad229146735d92e5383a662cdca))
* **ingestion:** unify Rejected/Exclude/worth-no into one concept + enrich-ask copy ([#209](https://github.com/powerset-co/powerpacks/issues/209)) ([a2d1cbf](https://github.com/powerset-co/powerpacks/commit/a2d1cbf4d26b6d1f0f55268c4ab9a3013d983047))
* **logbook:** add $logbook raw verbatim message archive skill ([#150](https://github.com/powerset-co/powerpacks/issues/150)) ([d4ae236](https://github.com/powerset-co/powerpacks/commit/d4ae236bab584074092ca9980935d16a6950698f))
* **powerset:** ShareOne-style one-URL install-powerpacks bootstrap skill ([#174](https://github.com/powerset-co/powerpacks/issues/174)) ([0b32f45](https://github.com/powerset-co/powerpacks/commit/0b32f455655578ebb1cfc7c299aafd5aa5c433b9))
* **search:** $recruit engine + consolidate search-* into $search (router, URL intake, routing eval) ([#153](https://github.com/powerset-co/powerpacks/issues/153)) ([ff89e59](https://github.com/powerset-co/powerpacks/commit/ff89e59030f27e52b173a0214fe48e2ddffa5d8f))
* **search:** agent-made decision contract replaces the route_query classifier ([#164](https://github.com/powerset-co/powerpacks/issues/164)) ([4bc3699](https://github.com/powerset-co/powerpacks/commit/4bc3699940d3b1798489e7f730427f6cc19e635d))
* **search:** geo-first JD sourcing — probes default to the JD's metro area ([#181](https://github.com/powerset-co/powerpacks/issues/181)) ([999f0d0](https://github.com/powerset-co/powerpacks/commit/999f0d07cb1f9cb5d3a975be53ab660d02677f85))
* **search:** plan critic at Review + opt-in micro-sort ordering pass ([#182](https://github.com/powerset-co/powerpacks/issues/182)) ([e585271](https://github.com/powerset-co/powerpacks/commit/e5852714b0cb82143d581cd63c4521f4fe3f146d))
* **search:** two-phase deep judging — triage filter by default, judge engine preference ([#168](https://github.com/powerset-co/powerpacks/issues/168)) ([045ef48](https://github.com/powerset-co/powerpacks/commit/045ef48fe94ac1185a6356395af1bb5cac6f633e))
* **setup:** ask about a Powerset account instead of silently defaulting ([#193](https://github.com/powerset-co/powerpacks/issues/193)) ([12f8e1d](https://github.com/powerset-co/powerpacks/commit/12f8e1d089ccd64407909d2f1bcedd445ada94cf))
* unify deep context review workflow ([e7f29ec](https://github.com/powerset-co/powerpacks/commit/e7f29ec0b27f6c131c2bccad0c0bed91311b9624))


### Bug Fixes

* **adapters:** drop restart/session guidance from installer output ([#176](https://github.com/powerset-co/powerpacks/issues/176)) ([c21b1f6](https://github.com/powerset-co/powerpacks/commit/c21b1f63759f788e337e06057c33f26895ab89b0))
* **adapters:** remove consolidated-away search skills on reinstall ([#173](https://github.com/powerset-co/powerpacks/issues/173)) ([1076dcf](https://github.com/powerset-co/powerpacks/commit/1076dcfe411753149894ea91231af0fb84882154))
* **adapters:** say what restart is actually for in installer output ([#175](https://github.com/powerset-co/powerpacks/issues/175)) ([8c1c2c8](https://github.com/powerset-co/powerpacks/commit/8c1c2c8df6ac19b80ff1a814fa4c08e814ab15e0))
* clarify Powerset setup endpoint ([#191](https://github.com/powerset-co/powerpacks/issues/191)) ([ef8289f](https://github.com/powerset-co/powerpacks/commit/ef8289f08ba5615bdc9c3c292b9a3e697f3f639f))
* **deep-context:** a reused enrichment receipt derives as completed ([#290](https://github.com/powerset-co/powerpacks/issues/290)) ([823cfd1](https://github.com/powerset-co/powerpacks/commit/823cfd175886233762e0e853395a62b15b884c2c))
* **deep-context:** always include iMessage groups; drop the scope prompt ([#243](https://github.com/powerset-co/powerpacks/issues/243)) ([1abddf8](https://github.com/powerset-co/powerpacks/commit/1abddf8bc9f0c5af7477123ad712884a2aec077f))
* **deep-context:** approve clicks survive the freshness observer ([#300](https://github.com/powerset-co/powerpacks/issues/300)) ([02cbddc](https://github.com/powerset-co/powerpacks/commit/02cbddc7b3c88f521098e58f8ae9719c3473627c))
* **deep-context:** card sections read Summary/Relationship; never show deep-research text ([#231](https://github.com/powerset-co/powerpacks/issues/231)) ([f532834](https://github.com/powerset-co/powerpacks/commit/f532834a17e66d43a6be323bed40dc4953481371))
* **deep-context:** close out realized retarget markers; surface stranded ones ([#252](https://github.com/powerset-co/powerpacks/issues/252)) ([6f7591d](https://github.com/powerset-co/powerpacks/commit/6f7591d79ba0a6acff632fa0e2ec478a30c003bf))
* **deep-context:** evict decided rows from status-filtered review tabs live ([#159](https://github.com/powerset-co/powerpacks/issues/159)) ([f566a7c](https://github.com/powerset-co/powerpacks/commit/f566a7cc51804593145a2a24608893207108af34))
* **deep-context:** explain sparse review cards ([22b1461](https://github.com/powerset-co/powerpacks/commit/22b14612d4958c8f19f0dd97e7336d8765b3215f))
* **deep-context:** ghost identities never reach the review queue ([#304](https://github.com/powerset-co/powerpacks/issues/304)) ([ff8d8b0](https://github.com/powerset-co/powerpacks/commit/ff8d8b0785f83f2c40347e3dbc794105c0c57928))
* **deep-context:** honor the clicked card when a pub has two parent owners ([#280](https://github.com/powerset-co/powerpacks/issues/280)) ([52aa310](https://github.com/powerset-co/powerpacks/commit/52aa31071b6001b014c92ed203cbf043c47803f1))
* **deep-context:** keep review stages live ([#220](https://github.com/powerset-co/powerpacks/issues/220)) ([87fd3c6](https://github.com/powerset-co/powerpacks/commit/87fd3c6913147a063d849838f279a726b1f0c913))
* **deep-context:** link-aware Summary fallback + strip meeting URLs from Contact ([#232](https://github.com/powerset-co/powerpacks/issues/232)) ([817aa8d](https://github.com/powerset-co/powerpacks/commit/817aa8d0b0c06bf7c3ff461ca5042ec2645ac3c9))
* **deep-context:** make synthesis the sole worth judge ([e0d6f91](https://github.com/powerset-co/powerpacks/commit/e0d6f91ff83c618915b44dadd1ddea2db7902990))
* **deep-context:** never rely on memory for approvals + register skill in Claude Code adapter ([#151](https://github.com/powerset-co/powerpacks/issues/151)) ([cbb63dd](https://github.com/powerset-co/powerpacks/commit/cbb63dd42485bc09e40aabbeb6d4037a80c62e8e))
* **deep-context:** one JSONL reader — splitlines() copies tore valid records ([#301](https://github.com/powerset-co/powerpacks/issues/301)) ([d1c1dce](https://github.com/powerset-co/powerpacks/commit/d1c1dceead279f570eac96e9c9a65b3ddaf2a63b))
* **deep-context:** one LinkedIn card per parent with selectable profile options ([#236](https://github.com/powerset-co/powerpacks/issues/236)) ([e84c9ef](https://github.com/powerset-co/powerpacks/commit/e84c9ef41f4622dd64e51c74991b09a441a975f0))
* **deep-context:** one worth-selection digest for review + enrichment ([#225](https://github.com/powerset-co/powerpacks/issues/225)) ([9f0aa0e](https://github.com/powerset-co/powerpacks/commit/9f0aa0ea4e82d68bd6625025b8ead056822b5989))
* **deep-context:** picking a profile option unions sibling contacts (no data loss) ([#240](https://github.com/powerset-co/powerpacks/issues/240)) ([67dd007](https://github.com/powerset-co/powerpacks/commit/67dd007e9a6114b4c86ade8e4dd172b75059e90d))
* **deep-context:** plain-text synthetic badge (drop the DNA emoji) ([#190](https://github.com/powerset-co/powerpacks/issues/190)) ([e7c0503](https://github.com/powerset-co/powerpacks/commit/e7c0503542afe62d80892d0db0eca4c949ae64ee))
* **deep-context:** quiet the focus ring on the worth search input ([#284](https://github.com/powerset-co/powerpacks/issues/284)) ([76158ae](https://github.com/powerset-co/powerpacks/commit/76158ae1b2914efd9c86308aba132ed8686b9f99))
* **deep-context:** re-key parent-scoped artifacts on merge so a merged person is one card ([#235](https://github.com/powerset-co/powerpacks/issues/235)) ([beaecd9](https://github.com/powerset-co/powerpacks/commit/beaecd915c2ed0253aca896c653cb4816f7463bb))
* **deep-context:** rejudge rebuilds raw bundles from current stores first ([#285](https://github.com/powerset-co/powerpacks/issues/285)) ([7650cd1](https://github.com/powerset-co/powerpacks/commit/7650cd122590a9e82c8987d2e9761cec9d2f4597))
* **deep-context:** restart is cleanup-only; user re-enters via bare $deep-context ([#262](https://github.com/powerset-co/powerpacks/issues/262)) ([201a15c](https://github.com/powerset-co/powerpacks/commit/201a15cb4a90682a73d33cde67a83003a4a248a9))
* **deep-context:** restart no longer wipes judged machine proposals ([#310](https://github.com/powerset-co/powerpacks/issues/310)) ([f88fe60](https://github.com/powerset-co/powerpacks/commit/f88fe60e74d94a5557dde2537047b6a20d06d20a))
* **deep-context:** restore clipped decision buttons on LinkedIn queue cards ([#241](https://github.com/powerset-co/powerpacks/issues/241)) ([a96b290](https://github.com/powerset-co/powerpacks/commit/a96b290007e2047d0b389ddfb569549ee3055e43))
* **deep-context:** restore scroll on tall LinkedIn queue cards ([#229](https://github.com/powerset-co/powerpacks/issues/229)) ([2bcfa52](https://github.com/powerset-co/powerpacks/commit/2bcfa5245d94ca06c8bc60b1c0e28417b826cd61))
* **deep-context:** restore the $deep-context restart route (small reset) ([#278](https://github.com/powerset-co/powerpacks/issues/278)) ([75ad509](https://github.com/powerset-co/powerpacks/commit/75ad50952bf368a2e66449133ae6c5158df9c9b8))
* **deep-context:** retarget judging is cached, concurrent, and heartbeats progress ([#288](https://github.com/powerset-co/powerpacks/issues/288)) ([3f7f30a](https://github.com/powerset-co/powerpacks/commit/3f7f30ad8b28a07e40e8cbfae9aa204a79b95ac4))
* **deep-context:** review restarts a stale running server instead of reusing it ([#246](https://github.com/powerset-co/powerpacks/issues/246)) ([9b99aa5](https://github.com/powerset-co/powerpacks/commit/9b99aa597b69fca50896e8b6ed1d833643c324ae))
* **deep-context:** route $deep-context clean to bin/clean-slate ([#273](https://github.com/powerset-co/powerpacks/issues/273)) ([7f34af5](https://github.com/powerset-co/powerpacks/commit/7f34af57f91af1ad857bd9ad2935772c403cbd3f))
* **deep-context:** route $deep-context restart to the human-reset primitive ([#261](https://github.com/powerset-co/powerpacks/issues/261)) ([52e0b42](https://github.com/powerset-co/powerpacks/commit/52e0b424cfcd48f134e727df0c54565c462ef5a1))
* **deep-context:** simplify LinkedIn correction review ([#219](https://github.com/powerset-co/powerpacks/issues/219)) ([b3a064a](https://github.com/powerset-co/powerpacks/commit/b3a064ae5c9b5746c718042bf58642b1e410de81))
* **deep-context:** simplify sparse context copy ([e039a8b](https://github.com/powerset-co/powerpacks/commit/e039a8b4fe2507816a219ad68dbd9440400ab200))
* **deep-context:** stage-complete clicks survive the freshness observer ([#291](https://github.com/powerset-co/powerpacks/issues/291)) ([2d7209b](https://github.com/powerset-co/powerpacks/commit/2d7209bcf19f74faa31ec3e8e9819754c834e8b1))
* **deep-context:** stale-message age renders with one decimal, never floored ([#296](https://github.com/powerset-co/powerpacks/issues/296)) ([fc7929e](https://github.com/powerset-co/powerpacks/commit/fc7929e75054070ec4a187b64f87286bc1188492))
* **deep-context:** stop confirming/announcing iMessage group inclusion ([#266](https://github.com/powerset-co/powerpacks/issues/266)) ([e9d4787](https://github.com/powerset-co/powerpacks/commit/e9d4787bc6f57d98e07d22eb19b431637d1d8997))
* **deep-context:** surface limbo maybes + re-judge them with verified-profile evidence ([#228](https://github.com/powerset-co/powerpacks/issues/228)) ([0f8dbfe](https://github.com/powerset-co/powerpacks/commit/0f8dbfed74ab1d6fb2f2c153807c5b057d9b723c))
* **deep-context:** the mailbox owner never reaches the worth review ([#305](https://github.com/powerset-co/powerpacks/issues/305)) ([31ab562](https://github.com/powerset-co/powerpacks/commit/31ab5625573aaf5824df07527ddc3e2757bed05f))
* **deep-context:** the server owns every stranded enrichment state ([#281](https://github.com/powerset-co/powerpacks/issues/281)) ([4ada9a8](https://github.com/powerset-co/powerpacks/commit/4ada9a8ae88b4d63cc2cac6348b9bf31ea3b6f3d))
* **deep-context:** the state token includes the pipeline-job bit ([#292](https://github.com/powerset-co/powerpacks/issues/292)) ([d1fad0b](https://github.com/powerset-co/powerpacks/commit/d1fad0b7b32aba0e82803b8ea9fd79a029f953c6))
* **deep-context:** unify synthetic card UI + guard summaries against empty profiles ([#234](https://github.com/powerset-co/powerpacks/issues/234)) ([acd63ad](https://github.com/powerset-co/powerpacks/commit/acd63ad4e8c74807ec24c9b632ddc8c7145bc509))
* **deep-context:** unstick the $0 all-reused enrichment continuation ([#267](https://github.com/powerset-co/powerpacks/issues/267)) ([f89345c](https://github.com/powerset-co/powerpacks/commit/f89345c6e06c1f56814d9c99e76efed7698d87eb))
* **deep-context:** verify completion against a fresh rebuild; retry failed bridge wakes ([#250](https://github.com/powerset-co/powerpacks/issues/250)) ([3d9e9f9](https://github.com/powerset-co/powerpacks/commit/3d9e9f99acac9903d9a2313fd4fbb1f7fe2e1881))
* **deep-context:** wire profile prefetch task ([#242](https://github.com/powerset-co/powerpacks/issues/242)) ([19cef1a](https://github.com/powerset-co/powerpacks/commit/19cef1a43570ad6c5e49ec88f9f3b2b6880e5d5f))
* **deep-context:** worth clicks patch worth_row; remove source chips ([#277](https://github.com/powerset-co/powerpacks/issues/277)) ([3900f9f](https://github.com/powerset-co/powerpacks/commit/3900f9fea0cdb0949e0c8386e11cfaf264279a4e))
* **deep-context:** worth clicks target the card's parent slug, not first-key-hit ([#297](https://github.com/powerset-co/powerpacks/issues/297)) ([93bf984](https://github.com/powerset-co/powerpacks/commit/93bf984342e68571daea3fc1eeebd98147b231da))
* **deep-context:** worth decisions write the row that attaches to the clicked parent ([#299](https://github.com/powerset-co/powerpacks/issues/299)) ([6e8d74c](https://github.com/powerset-co/powerpacks/commit/6e8d74c7d50b2453b80d8986fc6d78b994367844))
* **deep-context:** worth section is a pure view over facts verdicts ([#271](https://github.com/powerset-co/powerpacks/issues/271)) ([15711ed](https://github.com/powerset-co/powerpacks/commit/15711edba23629f8271f2cc7a3947c0a7b414516))
* **deep-context:** worth table search prefetches all rows on focus ([#298](https://github.com/powerset-co/powerpacks/issues/298)) ([7597091](https://github.com/powerset-co/powerpacks/commit/75970913363f63e993cf60e27c57705ddcf76f89))
* fail fast on expired Gmail authorization ([b6afb8d](https://github.com/powerset-co/powerpacks/commit/b6afb8de01f98fc10413f8fb25973f8832c609b6))
* **indexing:** fan-in no-op cache must fingerprint override files ([#172](https://github.com/powerset-co/powerpacks/issues/172)) ([0919b76](https://github.com/powerset-co/powerpacks/commit/0919b76ebe1c57dd6665a714bdcb8a7b4a534241))
* **indexing:** parallelize company cache reads ([#197](https://github.com/powerset-co/powerpacks/issues/197)) ([aabcf06](https://github.com/powerset-co/powerpacks/commit/aabcf06416d99a03bce930c57dd0e673b5a46358))
* **ingestion:** $setup runs the updater for the current harness, not always Codex ([#185](https://github.com/powerset-co/powerpacks/issues/185)) ([56828ed](https://github.com/powerset-co/powerpacks/commit/56828ed82fef9280c9e8c049cb153cf9cb03dbde))
* **ingestion:** LinkedIn connections are ground truth — machine no never drops them ([#212](https://github.com/powerset-co/powerpacks/issues/212)) ([01fdb83](https://github.com/powerset-co/powerpacks/commit/01fdb8329f56e8a49ee6b228863726c60ce9067d))
* **ingestion:** matched contacts fold their pre-match candidate identity ([#303](https://github.com/powerset-co/powerpacks/issues/303)) ([dd9f567](https://github.com/powerset-co/powerpacks/commit/dd9f567a90b5226ec7c7776e561249763b2b8ef9))
* **ingestion:** matched contacts keep one durable id across import runs ([#302](https://github.com/powerset-co/powerpacks/issues/302)) ([fc00a98](https://github.com/powerset-co/powerpacks/commit/fc00a98172d72c7d310a88743c020187c490e4fe))
* **ingestion:** one reset skill — all reset wording routes to $clean-slate ([#275](https://github.com/powerset-co/powerpacks/issues/275)) ([68ff994](https://github.com/powerset-co/powerpacks/commit/68ff994baef4f9091b1221300b2bfec61fa9d8df))
* **ingestion:** raise WhatsApp first-backfill cap to 3h + document duration ([#265](https://github.com/powerset-co/powerpacks/issues/265)) ([f11bc04](https://github.com/powerset-co/powerpacks/commit/f11bc0402942d2d50ce011763570174dece795de))
* **ingestion:** report wacli version + action in $update-powerpacks status ([#264](https://github.com/powerset-co/powerpacks/issues/264)) ([2786694](https://github.com/powerset-co/powerpacks/commit/2786694f57c92c0a76fbddb572093d845ec67c91))
* **ingestion:** retain WhatsApp DM contacts seen in groups ([f16cf6d](https://github.com/powerset-co/powerpacks/commit/f16cf6d6aa5bc039b3bb35947062013e9c7ced05))
* **ingestion:** review-UI live tab counts + rejected row state (post-209 polish) ([#210](https://github.com/powerset-co/powerpacks/issues/210)) ([7d7be6c](https://github.com/powerset-co/powerpacks/commit/7d7be6c55432ac144c4a27899c63f03e516a17cd))
* **powerset:** make updates deterministic ([#213](https://github.com/powerset-co/powerpacks/issues/213)) ([ba99954](https://github.com/powerset-co/powerpacks/commit/ba999542c6ec24eb528317f64664cbd335368a13))
* **powerset:** remove updater restart prompt ([#214](https://github.com/powerset-co/powerpacks/issues/214)) ([8eabd90](https://github.com/powerset-co/powerpacks/commit/8eabd90a935d8c841b548eca6d65aed533db00cc))
* **powerset:** update-powerpacks prints what actually changed ([#282](https://github.com/powerset-co/powerpacks/issues/282)) ([b96b276](https://github.com/powerset-co/powerpacks/commit/b96b276273567f18ca5f4c6a1fee5672925f92fd))
* **search:** Ashby JD fetch via posting API + narrow-pool preview nudge + skill polish ([#166](https://github.com/powerset-co/powerpacks/issues/166)) ([8c83f1c](https://github.com/powerset-co/powerpacks/commit/8c83f1c644c0bc303fc90b286f4b20e090c95347))
* **search:** CLI agent judges never bulk-filter — hard guard, no fallback ([#170](https://github.com/powerset-co/powerpacks/issues/170)) ([019c4cc](https://github.com/powerset-co/powerpacks/commit/019c4cca2ea39d732443b3e14f0ba7492aac6780))
* **search:** default the deep-mode judge to the gpt API and record judge identity ([#244](https://github.com/powerset-co/powerpacks/issues/244)) ([b9cb11a](https://github.com/powerset-co/powerpacks/commit/b9cb11aa2ec17515a220684bda284d25fc56f4e7))
* **search:** enforce location and repair anchor expansion ([#194](https://github.com/powerset-co/powerpacks/issues/194)) ([8761103](https://github.com/powerset-co/powerpacks/commit/8761103730feaddccb0c7a45f630b0a9b35fe9d4))
* **search:** explicit deterministic runtime cap for micro-sort (--max-batches) ([#184](https://github.com/powerset-co/powerpacks/issues/184)) ([2666f17](https://github.com/powerset-co/powerpacks/commit/2666f170e1e588eadcd217660fe6b909d0370d36))
* **search:** judge rubric enforcement rules + 0.55 sendable cut presentation ([#171](https://github.com/powerset-co/powerpacks/issues/171)) ([dea1e81](https://github.com/powerset-co/powerpacks/commit/dea1e81039c8c490b402153fd99a135950efc23a))
* **search:** make deep search recruiter-driven and plan-first ([#192](https://github.com/powerset-co/powerpacks/issues/192)) ([615e3bd](https://github.com/powerset-co/powerpacks/commit/615e3bd5fc20df5c8ab3b7aa7839f3a55212adac))
* **search:** make inferred-age filters visible end to end ([#227](https://github.com/powerset-co/powerpacks/issues/227)) ([978db22](https://github.com/powerset-co/powerpacks/commit/978db225a3b961b2357bd48cbd3f283140b37d04))
* **search:** retry errored judge verdicts; never cache a failure as a rejection ([#167](https://github.com/powerset-co/powerpacks/issues/167)) ([aff9b48](https://github.com/powerset-co/powerpacks/commit/aff9b48fdaf741687aed4c5f96bf1fcef8876815))
* **setup:** set realistic Modal indexing expectations ([#196](https://github.com/powerset-co/powerpacks/issues/196)) ([43a81e5](https://github.com/powerset-co/powerpacks/commit/43a81e5778919e1439dcf5d8c702eba60ff628f5))
* use wacli backfill for logbook deepen ([64c4826](https://github.com/powerset-co/powerpacks/commit/64c482691990eb2255a042c383e440fbd5b13efd))


### Performance Improvements

* **deep-context:** instant worth-card advance + no double CSV parse per click ([#247](https://github.com/powerset-co/powerpacks/issues/247)) ([bbc3554](https://github.com/powerset-co/powerpacks/commit/bbc35545900546c14aa8a20e112c42b922e062fc))


### Documentation

* **deep-context:** auto-approve reconcile under $25; cleaner cost prompt ([#239](https://github.com/powerset-co/powerpacks/issues/239)) ([51eaf07](https://github.com/powerset-co/powerpacks/commit/51eaf07fd448ea68082e66fa0559aa821724b28e))
* **deep-context:** auto-approve synthesis under $25; cleaner cost prompt ([#238](https://github.com/powerset-co/powerpacks/issues/238)) ([b1b8e58](https://github.com/powerset-co/powerpacks/commit/b1b8e5884f331398dc40f5dd4c6b5d9f42b7a9d5))
* **deep-context:** plan synthetic profiles via deep research ([#147](https://github.com/powerset-co/powerpacks/issues/147)) ([284fb98](https://github.com/powerset-co/powerpacks/commit/284fb98c442cdf35f3af398daeb0d1341e47fe6f))
* document $deep-setup and the import-&gt;process split across all guides ([#208](https://github.com/powerset-co/powerpacks/issues/208)) ([8c818e4](https://github.com/powerset-co/powerpacks/commit/8c818e40bba6822249d0d19168aa28e99007a83d))
* explain search, context, messaging, and indexing pipelines ([#195](https://github.com/powerset-co/powerpacks/issues/195)) ([a78d9f6](https://github.com/powerset-co/powerpacks/commit/a78d9f62bce18933b6860dc71b36f198233086b8))
* **ingestion:** drop stale Go-toolchain consent gate from import-messages guardrails ([#269](https://github.com/powerset-co/powerpacks/issues/269)) ([ae386f9](https://github.com/powerset-co/powerpacks/commit/ae386f926f5a791a20d6f121f9a105fa3c5fa066))
* install URL is now powerset.dev/powerpacks ([#178](https://github.com/powerset-co/powerpacks/issues/178)) ([e96762c](https://github.com/powerset-co/powerpacks/commit/e96762c779b80b4272307ae31722c45053c6d4e9))
* README install leads with the one-sentence install-powerpacks flow ([#177](https://github.com/powerset-co/powerpacks/issues/177)) ([6a05816](https://github.com/powerset-co/powerpacks/commit/6a0581618f36cf1a4d124f510fbadb65794be6a9))
* README search row describes the actual engine, not the search-highlight era ([#186](https://github.com/powerset-co/powerpacks/issues/186)) ([224cfbe](https://github.com/powerset-co/powerpacks/commit/224cfbe0244810700c8edbffbcc6550ea18a8379))
* require anonymizing contact PII in committed/shared dev artifacts ([#287](https://github.com/powerset-co/powerpacks/issues/287)) ([cc7192b](https://github.com/powerset-co/powerpacks/commit/cc7192bb33af124152d98fe74943664af8accf40))
* **search:** rename the "GATE 1" checkpoint to "Review" in the task list ([#180](https://github.com/powerset-co/powerpacks/issues/180)) ([64d4230](https://github.com/powerset-co/powerpacks/commit/64d4230e17f47ca713f5fde0cbfe35595bdd109a))
* **search:** retire slice-planning guidance ([#200](https://github.com/powerset-co/powerpacks/issues/200)) ([761c4c3](https://github.com/powerset-co/powerpacks/commit/761c4c39c9526fe531da0f4be07c14a34d5f56f0))
* simplify deep-context approval flow ([#198](https://github.com/powerset-co/powerpacks/issues/198)) ([3edb4a9](https://github.com/powerset-co/powerpacks/commit/3edb4a995cd89f9d4092316451a0829e15fb229e))


### Code Refactoring

* **deep-context:** replace the Codex bridge with review-status --wait ([#256](https://github.com/powerset-co/powerpacks/issues/256)) ([8892d17](https://github.com/powerset-co/powerpacks/commit/8892d175a3513a3391607cdf7d6a2d464cfea5ff))
* delete the retired research-review flow and the before_split fossil ([#315](https://github.com/powerset-co/powerpacks/issues/315)) ([fbad4a0](https://github.com/powerset-co/powerpacks/commit/fbad4a024afcc9b1e58349a32ecd23db6830bca4))
* **ingestion:** restructure the ingestion pipeline into stage-mirrored primitives ([#318](https://github.com/powerset-co/powerpacks/issues/318)) ([cb7a6ee](https://github.com/powerset-co/powerpacks/commit/cb7a6eeb528363631e97372e072cdcc87005c509))
* **search:** one orchestrator — fold local pipeline behind --backend, deep-on-local ([#165](https://github.com/powerset-co/powerpacks/issues/165)) ([53bc9e7](https://github.com/powerset-co/powerpacks/commit/53bc9e72b4749b8c4edb7ab22eb02e2b6abc96e2))
* **search:** single $search door — fold $recruit into deep mode (+ thin-JD guard, v2 scope) ([#163](https://github.com/powerset-co/powerpacks/issues/163)) ([98142ac](https://github.com/powerset-co/powerpacks/commit/98142ac0ad9ffa11ef2e833391683be7777325fe))

## [0.18.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.17.0...powerpacks-v0.18.0) (2026-06-25)


### Features

* **deep-context:** deep-research recovery also covers user-marked detaches ([aac16ff](https://github.com/powerset-co/powerpacks/commit/aac16ffd1e086cb899b183e6c40dab96b6db7da3))
* **deep-context:** exclude the mailbox owner's aliases from the network ([b43006e](https://github.com/powerset-co/powerpacks/commit/b43006e14b32a262d8a640ad0331d1be4e6d9922))
* **deep-context:** fold all clustered members into the parent (no needs_review limbo) ([612d04b](https://github.com/powerset-co/powerpacks/commit/612d04b2d1b5c993301250d6b560fc01c3dc2310))
* **deep-context:** keep-biased LinkedIn self-heal + parent-grouped review UI ([8f90ed4](https://github.com/powerset-co/powerpacks/commit/8f90ed40ee327537bcc07bf22f2362e1edb2a10e))
* **deep-context:** pass thread participants + detect owner aliases ([1f64505](https://github.com/powerset-co/powerpacks/commit/1f64505d9a50492c7c6acd70c825ddfdac835f7f))
* **deep-context:** recover contacts from the LinkedIn they shared themselves ([8c26492](https://github.com/powerset-co/powerpacks/commit/8c26492273d8d5904d489b73afaf1301d19a4fc1))
* **deep-context:** review UI defaults to Needs-review; rename Conflicts → Merged ([57c887d](https://github.com/powerset-co/powerpacks/commit/57c887db462d99ece9e80e6560008cf62a519f3c))
* **deep-context:** show LinkedIn avatars in review UI + collapse Merged rows first ([#145](https://github.com/powerset-co/powerpacks/issues/145)) ([304b2ed](https://github.com/powerset-co/powerpacks/commit/304b2edc7d75f51ad13f6e39d737e16b36068cdf))
* **deep-context:** treat LinkedIn Connections as ground-truth confirms ([2ab5373](https://github.com/powerset-co/powerpacks/commit/2ab5373764b4209e8e6cb311d3d782f32bcdaa61))
* **deep-context:** trim review tabs to Merged / Needs review / All ([893b4b1](https://github.com/powerset-co/powerpacks/commit/893b4b101fabeca775095a3c9a0679e361ecb5a7))


### Bug Fixes

* **deep-context:** don't let a self-reported retarget override a confirmed link ([30ed04e](https://github.com/powerset-co/powerpacks/commit/30ed04e52070609a50d0258841a4d9e026f3ec8d))
* **deep-context:** feed the judge the full LinkedIn work history (no cap) ([9eaab1e](https://github.com/powerset-co/powerpacks/commit/9eaab1e5a11c8470f40f32d269af9fe584ea81c9))
* **deep-context:** linear-time message collection + --force keeps the full checklist ([#146](https://github.com/powerset-co/powerpacks/issues/146)) ([31e78b1](https://github.com/powerset-co/powerpacks/commit/31e78b126a9fa80dc2097ccc69f5fe02e2539de6))
* **deep-context:** make `run`/`dry --help` safe (was running the pipeline) ([#149](https://github.com/powerset-co/powerpacks/issues/149)) ([746ea14](https://github.com/powerset-co/powerpacks/commit/746ea144d3f74fb354379aacd4efc94c7df14933))
* **deep-context:** match US WhatsApp numbers whose JID keeps the +1 ([#148](https://github.com/powerset-co/powerpacks/issues/148)) ([78cb2de](https://github.com/powerset-co/powerpacks/commit/78cb2deff401ebdeeb181d0e72597a80e1c56c8d))
* **deep-context:** propagate --force from `run` to synthesize ([1a60619](https://github.com/powerset-co/powerpacks/commit/1a606193f35ebb4fd542bc72e382748e0d86a599))
* **deep-context:** review UI shows the rich child dossier, not the parent stub ([55d4940](https://github.com/powerset-co/powerpacks/commit/55d49405145a4142c46530a3de39281335105a13))
* **network-import:** union resolved gmail emails onto the canonical row ([#144](https://github.com/powerset-co/powerpacks/issues/144)) ([57e5425](https://github.com/powerset-co/powerpacks/commit/57e5425a5b229be33be544bb8ddc5b797d549cdb))

## [0.17.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.16.0...powerpacks-v0.17.0) (2026-06-23)


### Features

* **deep-context:** cluster --dry-run (count pairs + cost) so the merge step has a real estimate ([#138](https://github.com/powerset-co/powerpacks/issues/138)) ([67afd0c](https://github.com/powerset-co/powerpacks/commit/67afd0c19ec3fbbf1403255f7b5b3a3cba7939d9))
* **deep-context:** owner profile as step 0 + Phase 4 (realize via fan-in + Modal index) ([#141](https://github.com/powerset-co/powerpacks/issues/141)) ([b22e68c](https://github.com/powerset-co/powerpacks/commit/b22e68c4cefde530bc215fa2dd8b0981900ee484))
* **deep-context:** scope deep research to parents with no kept link + stream live progress ([#139](https://github.com/powerset-co/powerpacks/issues/139)) ([091a3f6](https://github.com/powerset-co/powerpacks/commit/091a3f69ff025fb0d5105eedb11e53596984b0cf))


### Bug Fixes

* scrub real names/bio from test fixtures + skill examples ([#140](https://github.com/powerset-co/powerpacks/issues/140)) ([f447c0e](https://github.com/powerset-co/powerpacks/commit/f447c0e3011b174088127d04b2d40e5dd7f15683))


### Performance Improvements

* **deep-context:** default judge concurrency 16 -&gt; 64 (latency-bound, not CPU-bound) ([#142](https://github.com/powerset-co/powerpacks/issues/142)) ([9cfd8f8](https://github.com/powerset-co/powerpacks/commit/9cfd8f864b058ad5ca7d647093c2e5957060269a))


### Documentation

* **deep-context:** plain-language checklist (Dossiers-&gt;Context) + de-jargon step titles ([#136](https://github.com/powerset-co/powerpacks/issues/136)) ([8c6c108](https://github.com/powerset-co/powerpacks/commit/8c6c108a0f3212ffcb305494d8c6b25f4d958252))

## [0.16.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.15.0...powerpacks-v0.16.0) (2026-06-23)


### Features

* contact consolidation + single summary + review gate for deep-context self-heal ([#132](https://github.com/powerset-co/powerpacks/issues/132)) ([a3a41e7](https://github.com/powerset-co/powerpacks/commit/a3a41e79160e2f6f3a304259b316d24267f6c791))
* one editable decisions file — fold review items into the override, retire review-queue ([#133](https://github.com/powerset-co/powerpacks/issues/133)) ([c879402](https://github.com/powerset-co/powerpacks/commit/c87940211aa0e910d84d3a42fcc7d3092b61c2a1))


### Bug Fixes

* add oss community basics ([#122](https://github.com/powerset-co/powerpacks/issues/122)) ([4623517](https://github.com/powerset-co/powerpacks/commit/4623517913d8d2a91e52ba7b9e4882207c9081a1))
* **deep-context:** run echo points at summary.md + decisions table (not retired review-queue) ([#134](https://github.com/powerset-co/powerpacks/issues/134)) ([91ba97d](https://github.com/powerset-co/powerpacks/commit/91ba97deca50dd41882c28bae521a8a81fad4e94))
* require explicit hosted config ([305dbd4](https://github.com/powerset-co/powerpacks/commit/305dbd415655ec25944f9f615758da97d13cc3b9))
* scrub internal release references ([#121](https://github.com/powerset-co/powerpacks/issues/121)) ([7736e2b](https://github.com/powerset-co/powerpacks/commit/7736e2b2f9e5fa5238395997452521e2ed79a183))


### Documentation

* **deep-context:** phase-tag the checklist + fix stale P3.3 'backed up' line ([#129](https://github.com/powerset-co/powerpacks/issues/129)) ([96b212a](https://github.com/powerset-co/powerpacks/commit/96b212a239fcf693354a88b3d2641110049ccdff))

## [0.15.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.14.0...powerpacks-v0.15.0) (2026-06-23)


### Features

* deep-context Phase 3 — LinkedIn self-heal (verify/detach/retarget, durable override) ([#126](https://github.com/powerset-co/powerpacks/issues/126)) ([e50b104](https://github.com/powerset-co/powerpacks/commit/e50b10421de448d2380e394e63bee5460645fa9e))

## [0.14.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.13.0...powerpacks-v0.14.0) (2026-06-22)


### Features

* always promote unmerged people to canonical parents ([007ab62](https://github.com/powerset-co/powerpacks/commit/007ab625d625b16956170cec21d52ea32772a3b2))


### Bug Fixes

* register $deep-context skill in the Codex installer ([f797632](https://github.com/powerset-co/powerpacks/commit/f7976325a3bb08a1f029629c331d6a7b8e5cd0aa))

## [0.13.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.12.0...powerpacks-v0.13.0) (2026-06-22)


### Features

* complete canonical parent layer + idempotent dedup ([2b49128](https://github.com/powerset-co/powerpacks/commit/2b49128b01dffa6c45118c977a08008db184fac6))

## [0.12.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.11.0...powerpacks-v0.12.0) (2026-06-22)


### Features

* add $deep-context skill — per-person dossiers + LLM-judge merge ([0d410b5](https://github.com/powerset-co/powerpacks/commit/0d410b538e1a97cffc07ed0a685a35a6cb9142bd))

## [0.11.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.10.0...powerpacks-v0.11.0) (2026-06-21)


### Features

* run the A/B resolution as the next step of $enrich-email-markers ([0ea9162](https://github.com/powerset-co/powerpacks/commit/0ea91628e162ac32a44e11634fb0806c39a59731))
* run the A/B resolution as the next step of $enrich-email-markers ([dfb4d91](https://github.com/powerset-co/powerpacks/commit/dfb4d9129ca6c05637d69c771b09dd496ab03096))
* split $setup into per-source ingestion skills ([25b84d9](https://github.com/powerset-co/powerpacks/commit/25b84d95a41b6a0911cb0eb5c7920720dd20b0e4))

## [0.10.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.9.0...powerpacks-v0.10.0) (2026-06-20)


### Features

* **console:** settings page + per-source Gmail status ([b3984d4](https://github.com/powerset-co/powerpacks/commit/b3984d4f8722ddd1d3394b5cde6ac1cc7aaee2ae))

## [0.9.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.8.0...powerpacks-v0.9.0) (2026-06-19)


### Features

* add compare_resolution_ab primitive for A/B resolution diffs ([#96](https://github.com/powerset-co/powerpacks/issues/96)) ([0be8aab](https://github.com/powerset-co/powerpacks/commit/0be8aabf504d3bb223d101acb7eaacecf40d764e))
* add validate_search_index primitive and use it in $setup Step 8 ([#95](https://github.com/powerset-co/powerpacks/issues/95)) ([aea86e2](https://github.com/powerset-co/powerpacks/commit/aea86e2d8b81b745d9d6b1dbaeb1d560ab0983ac))
* console System page — self-update, secrets readiness, daemon reboot ([#85](https://github.com/powerset-co/powerpacks/issues/85)) ([6af94d9](https://github.com/powerset-co/powerpacks/commit/6af94d98d16a8eaf55ce16f8daec5441a61c493e))
* default infer_linkedin_markers to deterministic top-500 by volume ([#99](https://github.com/powerset-co/powerpacks/issues/99)) ([c2da2af](https://github.com/powerset-co/powerpacks/commit/c2da2af3a6ff63872a03a93c30d26856cebb4179))
* near-duplicate email filtering (Jaccard shingles) ([#107](https://github.com/powerset-co/powerpacks/issues/107)) ([b9fcb4b](https://github.com/powerset-co/powerpacks/commit/b9fcb4b1d7e25fdf2ca965d2c37d2f069dcb1d84))
* promote $setup to the unified LinkedIn+Gmail multi-source flow ([#97](https://github.com/powerset-co/powerpacks/issues/97)) ([4992e9b](https://github.com/powerset-co/powerpacks/commit/4992e9ba88a4a60993420e0cf6631b2e7f684bbc))
* remove the setup page; land on search (/) and gate the update nudge ([#89](https://github.com/powerset-co/powerpacks/issues/89)) ([0eea0b0](https://github.com/powerset-co/powerpacks/commit/0eea0b08f845e36d15b17c0fd5a57b230ecbabe8))
* rewrite $setup as a deterministic, rerunnable checklist ([#94](https://github.com/powerset-co/powerpacks/issues/94)) ([31dc5ee](https://github.com/powerset-co/powerpacks/commit/31dc5ee90d0c0ab7fe1ed4ee6de007d1aa391ccb))
* **setup:** local-only Messages (iMessage/WhatsApp) ingestion in $setup ([8dbcfa8](https://github.com/powerset-co/powerpacks/commit/8dbcfa85d3c7f8bd4d2f88e1aeb84fbf17cc3a48))
* signal-scored email selection (deterministic, no LLM) ([#104](https://github.com/powerset-co/powerpacks/issues/104)) ([dabf956](https://github.com/powerset-co/powerpacks/commit/dabf95661a07b2a33721c5921ceb962a358d59d5))
* simplify marker schema (employers list, drop is_person/relationship) ([77bb7cb](https://github.com/powerset-co/powerpacks/commit/77bb7cb686544e3fa42a3e58c0e59a7c1a52efaf))
* simplify marker schema (employers list, drop is_person/relationship) ([b583a12](https://github.com/powerset-co/powerpacks/commit/b583a1289ba92a9b1260a19b5ef8ff516d1b769c))
* smart email-context sampler (thread-dedup + sent-preferred, default 20) ([#103](https://github.com/powerset-co/powerpacks/issues/103)) ([7fe359d](https://github.com/powerset-co/powerpacks/commit/7fe359d132bdd951c9d73adf4acb9d5e67d065b6))
* tell the marker LLM who the mailbox owner is (from msgvault) ([b0842d7](https://github.com/powerset-co/powerpacks/commit/b0842d71394ca825e54c2e78b24641f7f806664d))
* tell the marker LLM who the mailbox owner is (from msgvault) ([3ea01b8](https://github.com/powerset-co/powerpacks/commit/3ea01b8cadc2d37e87f412a9c57b6895630f0a68))


### Bug Fixes

* bin/launch opens the onboarding wizard for new operators ([#90](https://github.com/powerset-co/powerpacks/issues/90)) ([ec53fb2](https://github.com/powerset-co/powerpacks/commit/ec53fb2bd1aacc616fe76916da2a5aa6fd05d4b3))
* count real CSV records in people_csv_rows (not lines) ([#98](https://github.com/powerset-co/powerpacks/issues/98)) ([5a51043](https://github.com/powerset-co/powerpacks/commit/5a5104367dc1b2f55e2939a75aaafd33939b1c0b))
* drop duplicate canonical_name marker category ([813e30c](https://github.com/powerset-co/powerpacks/commit/813e30ca552812115b6b4097d65ff6632347c1ff))
* drop duplicate canonical_name marker column ([e3cae93](https://github.com/powerset-co/powerpacks/commit/e3cae937f57d4f38f8da4ba0c8e658e842da2ebd))
* gate marker step on phase-1 success + schema guardrail ([04a4eb8](https://github.com/powerset-co/powerpacks/commit/04a4eb871bc728c31aaf1ea435214fd110ecd9b3))
* gate marker step on phase-1 success + schema guardrail in skill ([6a52941](https://github.com/powerset-co/powerpacks/commit/6a5294119f029f5a71644671601aa48a6e77847a))
* hardcode $enrich-email-markers concurrency to 12 ([7565c17](https://github.com/powerset-co/powerpacks/commit/7565c1771971f9449ab782a2a362b3658b7075c5))
* hydrate local search index when modal pipeline is a noop ([#87](https://github.com/powerset-co/powerpacks/issues/87)) ([b03f18d](https://github.com/powerset-co/powerpacks/commit/b03f18d77c87fda1e606d815c3a45b3bf4faf322))
* index-direct email lookup + safe default concurrency for $enrich-email-markers ([#108](https://github.com/powerset-co/powerpacks/issues/108)) ([bdce4bf](https://github.com/powerset-co/powerpacks/commit/bdce4bf07fef36c746e55156a490108481eb53aa))
* onboarding 'Next — try a search' becomes a primary CTA once indexing completes ([#91](https://github.com/powerset-co/powerpacks/issues/91)) ([ae07cd6](https://github.com/powerset-co/powerpacks/commit/ae07cd690dd482422250731ffcc0f88ad42f6e2b))
* per-source status loading (sources tab + LinkedIn source status) ([#88](https://github.com/powerset-co/powerpacks/issues/88)) ([20e4ad3](https://github.com/powerset-co/powerpacks/commit/20e4ad3757b307608e301112c66edc260b6a474f))
* route all CSV reads through a central CsvIO to lift the 128KB field limit ([#106](https://github.com/powerset-co/powerpacks/issues/106)) ([09a309e](https://github.com/powerset-co/powerpacks/commit/09a309e39e2ce9f21c7f11dc5e3a83d43c88faf2))
* **setup:** bound msgvault re-auth and add an explicit sync-window prompt ([#105](https://github.com/powerset-co/powerpacks/issues/105)) ([f21bf79](https://github.com/powerset-co/powerpacks/commit/f21bf799134f4bb740a71c82f9d8baf323b5eed0))
* trim System page to update + daemon; pin sidebar so only content scrolls ([#92](https://github.com/powerset-co/powerpacks/issues/92)) ([16376c1](https://github.com/powerset-co/powerpacks/commit/16376c10f703d4168e1f2ab52c6db18256de3e73))


### Performance Improvements

* **indexing:** pace RapidAPI company fetches at 300 rpm ([ac6990d](https://github.com/powerset-co/powerpacks/commit/ac6990d5f459ec42d31db9d5dd375651bd96ada5))
* stream msgvault contact aggregation (O(messages) -&gt; O(contacts) memory) ([#100](https://github.com/powerset-co/powerpacks/issues/100)) ([b1b3e21](https://github.com/powerset-co/powerpacks/commit/b1b3e21bfce9abe903d9902e3a1be8aa0061f243))
* windowed-streamed email-context fetch (2.5x faster, -40% RSS) ([626701b](https://github.com/powerset-co/powerpacks/commit/626701b3af272fbd0faf60043fb298901241e34f))
* windowed-streamed email-context fetch + hardcode marker concurrency ([8ed8274](https://github.com/powerset-co/powerpacks/commit/8ed827434c272d622e3701f58c05114a7ff6a215))


### Documentation

* **setup:** forbid raw msgvault sync; recover by syncing less, not more ([#101](https://github.com/powerset-co/powerpacks/issues/101)) ([bb41c8a](https://github.com/powerset-co/powerpacks/commit/bb41c8a56cd2c706b3e5262e9290294e3a2b8136))

## [0.8.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-v0.7.0...powerpacks-v0.8.0) (2026-06-17)


### Features

* add email-context + LinkedIn marker enrichment primitives ([#82](https://github.com/powerset-co/powerpacks/issues/82)) ([1ac0951](https://github.com/powerset-co/powerpacks/commit/1ac0951853f8a215628088e82a3606e08151468f))
* auto-open markers.csv via --open flag ([#84](https://github.com/powerset-co/powerpacks/issues/84)) ([ae18e6a](https://github.com/powerset-co/powerpacks/commit/ae18e6a41db1d6840aa3beaaa5fc442dce8d42db))
* seed Gmail Process status on click + add no-spend stub mode ([2f61315](https://github.com/powerset-co/powerpacks/commit/2f61315ba8a75cad6b1e23f8c7b06971009cdc98))
* seed Gmail Process status on click + no-spend stub mode ([971b834](https://github.com/powerset-co/powerpacks/commit/971b834388c325f9b83a78e8037d50717459ea59))


### Bug Fixes

* keep Gmail process on canonical people csv ([390d49b](https://github.com/powerset-co/powerpacks/commit/390d49b4ae92682f1768ed9cc55768dc9887f0f0))
* remove Gmail Process stub flag (keep seed + 3-stage progress) ([e814ca2](https://github.com/powerset-co/powerpacks/commit/e814ca2329f04eca00193fd24c2740838218e0f1))
* remove Gmail Process stub flag (kept seed + 3-stage progress) ([03bb5f1](https://github.com/powerset-co/powerpacks/commit/03bb5f1a68f9a7adfdb75ef3c056e172875128c8))
* remove legacy GCP bootstrap provisioning ([f115be6](https://github.com/powerset-co/powerpacks/commit/f115be67cd3dd72bfe71e7f5b793c267b3fc387f))

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
