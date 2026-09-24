# Share + upload: who leaves the laptop, and how it reaches Powerset

Created: 2026-09-24

Change log:
- 2026-09-24: first version — labels (Jev), tags (human), share list, upload to
  Powerset (TurboPuffer + Postgres). Written before the code; edit as the code lands.
- 2026-09-24 (after the code): corrected the facts the builders found wrong from
  the artifacts (parent-id keying, 548 slugs, live namespaces narrower than the
  contracts, `_dev` twins), dropped `relationship_active` (cadence is
  deterministic) and dated Jev requests by their evidence, scoped cloud tag
  deletes to the share list, resolved cloud ids by slug, recorded the
  company/school id gap.

## Why

Today every Powerpacks user's LinkedIn network lives in the cloud; some users
also have Gmail/iMessage/WhatsApp people there. Processing is moving onto the
laptop (`$deep-context`), so the cloud becomes a **hub** the laptop uploads a
*chosen subset* of its network to. Two missing pieces:

1. **Share** — a per-person decision "does this person leave my laptop", made
   from ~50 cheap machine labels (family / homie / professional / private …)
   the user can override with tags.
2. **Upload** — push the shared people's already-built local index into the
   cloud shape `$search powerset` reads.

## Verified facts the design rests on (2026-09-24, read from the artifacts)

Local:
- `merged/people.csv` (766 rows here): 37 columns, `packs/ingestion/schemas/people_schema.py:44-99`.
  `id = uuid5(6ba7b810-…, "linkedin:<slug>")` when a slug exists — **the same recipe as cloud
  `persons.id`** (`network-search-api/shared/ids.py:21,36`); 28 of 50,761 cloud rows were
  minted under another id, 0 of them in this network. 548/766 rows have a slug; the 218
  without one are keyed `candidate:email:<address>` (unresolved Gmail candidates).
- `.powerpacks/search-index/local-search.duckdb` is built from the TurboPuffer namespace
  contracts (`scripts/build-local-duckdb-shim.py:74-88`): `local_people_positions` ↔
  `aleph_people_v1`, `local_summaries` ↔ `aleph_summaries_v1`, `local_companies` ↔
  `aleph_companies_v1`, `local_people_education` ↔ `aleph_people_education_v1`,
  `local_education` ↔ `aleph_education_v1`. Column names are identical; vectors are
  1536-d `text-embedding-3-small`, cosine — same as cloud. `local_person_profiles`
  (39 cols, incl. `hydrated_context` JSON) mirrors Postgres `persons`. The **live**
  namespaces are strictly narrower than the checked-in contracts (no `person_id`,
  `title_hash`, `company_*`, `*_followers` on `aleph_people_v1`; no `base_id` on
  summaries/education), so an upload writes `contract ∩ local table ∩ live schema`.
  Local `allowed_operator_ids` holds a placeholder operator uuid; the upload always
  overwrites it. Local company/school ids are `uuid5(indexing-ns, "company:…")`; the cloud
  keys `aleph_companies_v1` by `urn:harmonic:company:<n>` — **the two never collide** (see
  Open).
- Deep-context leaves per person are keyed by **parent id** (`parent-<12hex>`), not the
  people.csv id; `review_store.parent_ids_by_person(index.json)` is the one map:
  `deep-context/facts/<parent_id>.jsonl` (strict schema, `synthesize_person_context.py:253-337`;
  `relationship_category` present in only 14/552 files, `is_owner` true in 0),
  `deep-context/dossiers/<slug>.md` and `parents/<slug>.md` (YAML front matter incl.
  `generated_at` + sections, `compose_dossier.py:201-294`), `deep-context/raw/<parent_id>.json`
  (`direction: from_me|from_them|from_other`, a capped sample — body-free fields only may be
  used), `people.csv.interaction_counts/last_interaction`. The human/mirror worth row is
  keyed `parent-worth:<parent_id>`.
- Human decisions live in `network-import/overrides/review.csv` (`review_store.py:40-77`);
  human > machine (`worth_view.py`). Nothing named private/tag/label/warmth exists locally.
- Jev/TypeSafe client `packs/search/primitives/llm_rerank_candidates/jev/client.py`:
  `POST https://api.typesafe.ai/v1/systemone`, model `jev-1.13.0`, question types
  `noul` (probability), `choice` (probabilities over named options), `score` (over ordinal
  levels); $0.042 per 1M input tokens; per-request sha cache under `<out>/jev/`; usage rows
  via `append_usage_row`. `_request()` (:219-263) is question-agnostic; `score_candidates`
  is hard-wired to the JD question set — the seam is `score_one` (:313-339).

Cloud (production, read-only checks):
- `persons`: PK `id`, **UNIQUE `public_identifier` (NOT NULL)**, UNIQUE `public_profile_url`;
  50,761 rows, 50,551 with `hydrated_context`. Upsert SQL the cloud pipeline uses:
  `network-search-api/data_pipeline_v2/.../sync_persons_to_supabase.py:70-115`
  (`ON CONFLICT (public_identifier) DO UPDATE SET col = COALESCE(EXCLUDED.col, persons.col)`).
- `operator_person_sources(id, operator_id varchar, person_id uuid FK persons ON DELETE CASCADE,
  source_channel varchar, source_identifier varchar, discovery_method varchar,
  relationship_strength float, total_interactions int, messages_sent int, messages_received int,
  first_interaction_at, last_interaction_at, gmail_token_id, discovered_at, created_at, updated_at)`,
  UNIQUE `(operator_id, person_id, source_channel, source_identifier)`. Existing `source_channel`
  values: `whatsapp, gmail, phone, csv_import, messages_research, linkedin, contacts, calendar`.
- TurboPuffer `aleph_people_v1` live attrs: `id, vector, word_tokens, char_tokens, d2q_tokens,
  phrase_tokens, position_title, seniority_band, company_id, city, state, country, macro_region,
  is_current, total_years_experience, start_date_epoch, end_date_epoch, tenure_years,
  inferred_birth_year, base_id, role_track, metro_areas, allowed_operator_ids, role_ids`
  (+ `taste_*`). Doc id = `"<person uuid>-<position idx>"`. Set scoping =
  `allowed_operator_ids ContainsAny <users.id of set members>`; the cloud writer derives
  `allowed_operator_ids` from ALL of `operator_person_sources` (`upload_people_turbopuffer.py:198-217`).
  Every namespace has a `_dev` twin (`ALEPH_ENV=staging`).
- `contact_tags(operator_id uuid, person_id uuid NULL, group_key text, tag ∈ private|skip)`,
  `PUT/DELETE /v2/contacts/tags` (`api_v2/routes/contacts.py:639,690`). Search drops a person
  when every set member who has them tagged `private` (`contact_tag_filter.py`).
- My operator: `users.id = 274ac942-3377-4401-886f-88c994985e1b`; 15 `operator_person_sources`
  rows (contacts/calendar/gmail 5 each) — my 766-person local network is not in the cloud.
- The laptop `.env` DSN is the `postgres` role with INSERT/UPDATE/DELETE on all of the above.

## Decisions

1. **Labels are a stage, `share`, under `packs/ingestion/primitives/share/`.** Output dir
   `.powerpacks/share/` with `labels.csv` (machine), `tags.csv` (human), `share.csv`
   (derived), `manifest.json`, `jev/<sha>.json` (client cache). Manifest + outputs only.
2. **The judge is Jev** with a frozen 34-question set (`share/questions.py`) plus 10
   deterministic labels computed from body-free metadata. The request's `reference_date` is
   the dossier's `generated_at`, so the per-request cache only misses when evidence changes. Input per person = dossier markdown
   (parent dossier if present, else child) + the facts JSON + profile fields + channel/cadence
   metadata + owner context. **Never raw bodies.** Only people with a facts file or dossier are
   sent; LinkedIn-only people get deterministic labels (`linkedin_only`).
   Cost: ~1–3k input tokens/person → ~$0.05 for 766 people. Still spend-gated
   (`--approve-spend`, `common/gates.py`), estimate printed first.
3. **Human tags win.** `tags.csv` rows: `person_id, tags, note, updated_at`; tags are a
   `|`-joined set from the same label vocabulary plus `private` and `share`. Machine never
   writes `tags.csv`. Set with `bin/deep-context tag <lookup> +private -friend`.
4. **`private` is the one blocking label.** Auto-suggested (`private_suggested`) when:
   family, romantic partner, minor, sensitive context (health/legal/finance/immigration/
   romance/family conflict), healthcare/legal/financial service provider, or `is_owner`.
   Suggested private blocks upload until a human tags `share`. `confidential_dealings` is
   a label only — on this network it fired on 87 people, 50 of them recruiters.
5. **`share.csv` is the contract between the two halves**, one row per `people.csv` row:
   `person_id, public_identifier, share (yes|no), reason, labels (|-joined active labels),
   source (human|machine), updated_at`. Reasons, first rule wins: `owner`, `human_private`,
   `human_share`, `private_suggested`, `automated_sender` (p ≥ 0.6), `stranger` (p ≥ 0.6),
   `default` (yes). `share` refuses to write a list that does not cover every people.csv
   row — the upload reconciles the cloud to it, so a partial list would un-share the rest. Everything else defaults to yes — same default as today's whole-CSV
   LinkedIn upload. Rows keyed by a `superseded_person_ids` member follow the surviving row.
6. **Upload = reconcile, not append.** `packs/indexing/primitives/upload_powerset/` reads
   `share.csv` + the DuckDB and makes the cloud state for THIS operator equal the share list:
   - `persons`: upsert the shared people **that have a `public_identifier`** with the cloud
     pipeline's column list and the COALESCE turned around — `COALESCE(persons.col,
     EXCLUDED.col)`: the cloud owns a person it already has, a laptop only fills NULLs;
     `hydrated_context` from `local_person_profiles`. People without a slug cannot enter `persons` (NOT NULL/UNIQUE)
     and are reported as `skipped_no_linkedin` (count only — their keys are email addresses).
     The cloud id is looked up by slug; where it differs from the local uuid5, every
     cloud-keyed write (sources, tags, patches) uses the cloud id.
   - `operator_person_sources`: desired rows = one per (person, local source channel) with
     `discovery_method = 'powerpacks'`, `source_channel ∈ gmail|imessage|whatsapp|linkedin`,
     `total_interactions`/`last_interaction_at` from `people.csv`; upsert (counts refresh only
     on rows this method wrote), delete this operator's `powerpacks` rows that are no longer
     desired (un-share). Another method's row with the same key is left alone.
   - TurboPuffer, 5 namespaces: for people **new to the cloud**, upsert the local docs (same
     columns, `allowed_operator_ids` = union from `operator_person_sources` after the PG step);
     for people **already in the cloud**, patch `allowed_operator_ids` only (`patch_rows`) —
     never regress cloud-enriched attributes. Un-share = patch the operator out.
   - `contact_tags`: `private` row for people whose share reason is `human_private` or
     `private_suggested` and who exist in `persons`. A cloud tag is a human decision (the
     Powerset UI): it is deleted only where the human's local word is `share`, never by a
     machine default.
   - Operator id = `users.id` for the credentials JWT `sub` (`postgres_client.credentials_subject`).
   - Without `--apply` the run plans only (counts per table, first N ids, the Postgres host
     and namespaces it would write); `--apply` writes. `ALEPH_ENV=staging` redirects
     TurboPuffer to the `_dev` twins — there is one Postgres, and the plan names its host.
   - Runs on the laptop (`.env` creds) or in the Modal sandbox (`linkedin_modal_pipeline.py
     upload-powerset` → `run_upload.py` shelling to the same module against the volume's
     `runs/<label>/local-search.duckdb`, secrets from the workspace; only the slug-bearing
     rows of share.csv are copied to the volume; `ALEPH_ENV` is forwarded; the operator id
     is derived from the laptop's credentials unless given).
7. **No new nouns in user-facing text**: label, tag, private, share, upload, hub = Powerset.
   No UI in this round.

## Question set (labels.csv columns)

Deterministic (no LLM): `cadence` (dormant >2y | stale >1y | occasional | regular | frequent,
from counts + last_interaction), `recency_days`, `channels` (|-joined), `direction`
(they_initiate | i_initiate | mutual, from raw bundle `direction` counts), `group_chat_only`,
`linkedin_only`, `network_worth` (pass-through effective worth), `is_owner`,
`shared_employer`, `shared_school` (from facts `shared_context[].overlap`).

Jev — choice: `relationship_kind` (family | romantic_partner | close_friend | friend |
acquaintance | colleague | business_contact | service_provider | community | stranger |
unknown), `mode` (professional_only | personal_only | mixed | unknown), `hierarchy`
(manager | peer | report | none | unknown), `intro_source` (work | school | mutual_friend |
family | online | event | cold_outreach | unknown), `seniority` (executive | senior | mid |
junior | student | retired | unknown), `function` (engineering | product | design | sales |
marketing | operations | finance | legal | people | founder_exec | investor | academic |
healthcare | creative | government | other | unknown).
Jev — score: `warmth` 0–4 (none/automated → distant acquaintance → friendly → close → inner circle).
Jev — noul (probability columns): `is_family`, `is_close_friend`, `is_personal`,
`is_professional`, `is_service_provider`, `is_transactional`, `is_automated_sender`,
`is_stranger`, `is_recruiter`, `is_investor`, `is_founder`, `is_coworker_current`,
`is_coworker_past`, `is_classmate`, `is_client`, `is_vendor_or_partner`, `is_mentor_or_advisor`,
`is_mentee_or_report`, `is_neighbor_or_local`, `is_healthcare_legal_or_financial_provider`,
`sensitive_context`, `is_minor`, `confidential_dealings`, `owner_would_intro`,
`they_would_take_owner_call`, `met_in_person`, `notable`. (`reciprocal` was dropped: the
deterministic `direction` label is the same counts.)
Derived: `private_suggested` (bool) + `private_reason` (which rule fired).

Threshold for a noul label to be "active" in `share.csv.labels`: p ≥ 0.6. Choice/score:
argmax. All thresholds live in one table in `share/labels.py`.

## Files

Agent A (share stage):
- `packs/ingestion/primitives/share/{__init__,models,questions,evidence,labels,tags,share}.py`,
  `share/README.md` (mermaid + file table), tests `tests/test_share_*.py`.
- `packs/search/primitives/llm_rerank_candidates/jev/client.py`: lift `score_one` into a
  request-keyed `answer(request, *, output_dir, api_key, client, concurrency)` used by both
  `score_candidates` and the share stage. Byte-identical JD behavior (existing tests + the
  frozen-contract hashes must pass untouched).
- `bin/deep-context`: `label`, `tag`, `share` passthroughs.

Agent B (upload):
- `packs/indexing/primitives/upload_powerset/{upload_powerset,models,plan,postgres,turbopuffer}.py`
  + `README.md`, tests `tests/test_upload_powerset.py` (fake PG cursor + fake TP namespace;
  no network).
- `packs/indexing/modal/linkedin_modal_pipeline.py`: `upload-powerset` subcommand;
  `packs/indexing/modal/run_upload.py` sandbox entry.

After both land: `packs/ingestion/skills/deep-context/SKILL.md` step 9 (label → tag → share →
upload), CLAUDE.md routing line, `packs/indexing/README.md` row.

## Verification (real surface)

1. `bin/deep-context label --estimate` on the real install → count + $ estimate.
2. `bin/deep-context label --approve-spend` (≈$0.05) → `labels.csv` 766 rows; spot-check 10
   people by hand against their dossiers (family/homie/service correctly separated;
   `private_suggested` fires on the obvious ones).
3. `bin/deep-context tag --name "<someone>" +private` → `tags.csv` row; `share` → `share.csv`
   flips that row to `no/private`.
4. `upload_powerset.py` (no flag = plan only) → plan counts (persons upserts, OPS
   inserts/deletes, TP upserts/patches per namespace, skipped_no_linkedin).
5. Real write ONLY after explicit go, first against `ALEPH_ENV=staging` (`_dev` namespaces);
   Postgres rows are scoped to operator `274ac942…`. Then `$search powerset` on a query that
   should hit a newly shared person.

## Open for Arthur

- **Company/school ids don't cross the boundary.** Position docs for a person new to the
  cloud carry a local `company_id` no cloud company doc has, and the upload gap-fills a
  company doc under that local id — a second doc for a company the cloud already knows
  under its harmonic URN. Invisible today (all 192 cloud-new people here have zero
  positions) but the first LinkedIn-resolved newcomer with work history hits it. The fix
  belongs to whoever owns company identity: resolve local companies against
  `aleph_companies_v1` by name/LinkedIn URL before upload.

- Default share = yes for unflagged people (matches today's whole-CSV upload). Flip to
  opt-in if you'd rather.
- People without a LinkedIn slug never reach `persons`/TurboPuffer (cloud constraint). They
  stay local until the cloud grows a non-LinkedIn person key.
- Dossier text goes to TypeSafe for labeling (synthesized facts, not bodies). Say if that
  provider boundary is not acceptable; the fallback is the same questions through OpenAI.
