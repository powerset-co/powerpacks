# Console: My Contacts + Local Search plan 🗺️

Created: 2026-06-11

## Change log
- 2026-06-11: Initial plan (investigation of app/ + network-search-app + DuckDB schema).
- 2026-06-11: Phases 1–3 implemented via parallel agent team + launchd daemon + plugin refactor. See Review.
- 2026-07-23: OBSOLETE — the powerpacks-console app (`app/`) and
  `scripts/run-powerpacks-console.sh` were deleted from the repo; the console
  is no longer a supported surface. Console sections below are historical, and
  all unchecked console items are cancelled.

## Goal
Bring `app/` (powerpacks-console) close to network-search-app for two surfaces:
**My Contacts** (browse `local_person_profiles` DuckDB table with `email:`-style
operators) and **Local Search** (fully local `local_search_pipeline` runs from
the UI). No new Python server.

## Architecture decision (no new server ✅)
The console already has the right shape: a Vite-plugin local API
(`app/local-api/powerpacksLocalApiPlugin.ts`) that serves `/local-api/*` from
Vite's own dev middleware, reads `.powerpacks/` state files, and shells out to
Python via `uv run --project . python ...` (sync for quick reads — see
`localDuckdbTableCounts()` with its mtime-keyed cache — and async jobs with
polled status/logs for long pipeline runs). We compose everything from these
existing primitives:

- Contacts/profile reads → small read-only DuckDB queries via the existing
  python-subprocess pattern (500 profiles; latency fine; keep the mtime cache).
  If spawn latency ever annoys, swap to `@duckdb/node-api` inside the plugin —
  still no server. Not needed for v1.
- Local search → spawn `local_search_pipeline.py run --db ...` as a background
  job writing a normal `.powerpacks/runs/` run dir; the existing run sidebar +
  `LocalResultsTable` + progress polling render it unchanged. Never touches
  TurboPuffer/Postgres. ⚠️ Each search spends one LLM expansion call (cents) —
  surface that in the UI, no silent spend.

## Data available (local-search.duckdb)
- `local_person_profiles` (500): full_name, headline, current_title/company,
  city/state/location_raw, all_emails, all_phones, primary_email/phone,
  linkedin_url, x_twitter_handle, twitter_handle, profile_picture_url,
  source_channels, summary, work_experiences, education — everything the
  ContactsV2 table + PersonDetails page show.
- `local_people_positions` (3,909), `local_people_education` (1,018),
  `local_summaries` (500), `local_companies` (1,943) for the profile view.

## Query operators (mirror `$search-contacts` contract, extend for local)
From `packs/contacts/skills/search-contacts/SKILL.md`: plain text → name;
`email:`, `phone:` (bare `phone:` = has phone), `headline:`, `company:`.
Local additions requested: `twitter:` (bare = has handle), plus cheap
wins `city:`/`location:`. Tokenizer: split on spaces outside `key:` prefixes;
multiple terms AND together.

## Phases

### Phase 1 — My Contacts (replace `LocalContactsPage` stub)
- [ ] Plugin: `GET /local-api/contacts?q=&sort=&dir=&page=` → parse operators →
      parameterized SQL over `local_person_profiles` → `{rows, total, page}`
- [ ] UI: mirror ContactsV2 — columns Name (avatar+link), Headline, Location,
      Email badge, LinkedIn icon, X handle, source badges; URL-driven
      q/sort/dir/page; 50/page; debounced (300ms) search box
- [ ] Empty/edge states: no duckdb file yet → point at Setup/Index tab

### Phase 2 — Profile view (mirror PersonDetails)
- [ ] Plugin: `GET /local-api/contacts/person/:person_id` → profile row +
      positions (joined company info, sorted, is_current → "Present") +
      education + summary
- [ ] UI: identity card (name, socials, emails, phones, location), work
      experience timeline, education, summary; route `/contacts/:personId`

### Phase 3 — Local Search from the UI
- [ ] Plugin: `POST /local-api/search/local-run {query}` → async job spawning
      `local_search_pipeline.py run --db .powerpacks/search-index/local-search.duckdb`
      into a standard `.powerpacks/runs/` dir (reuses job/log plumbing)
- [ ] UI: search input on the runs view; new run appears in existing sidebar;
      results render through existing `LocalResultsTable`
- [ ] Show expansion output (filters/traits) via existing
      `LocalQueryExpansionPanel`; label LLM spend per search

### Phase 4 — Parity polish (later)
- [ ] Filter pills with remove buttons (PowerSearchV2 style), recount on edit
- [ ] CSV export of contacts/results; mobile card layout

## Verification
- [ ] Unit tests for operator parser (plugin-side) if logic lands in TS;
      `uv run --project . python -m unittest discover -s tests` stays green
- [ ] Manual: `email:@gmail.com`, `company:google`, `twitter:`, sort/page URLs
- [ ] Local search run produces results with zero TurboPuffer/Postgres traffic

## Review (2026-06-11) ✅

Done (uncommitted, working tree):
- [x] Refactor: `powerpacksLocalApiPlugin.ts` (3,122 ln) → 44-ln registrar +
      `lib/*` helpers + `routes/{setup,runs,env,onboarding,messages,contacts,personDetails,localSearch}.ts`;
      new parameterized `queryLocalDuckdb()`; all existing routes curl-verified identical.
- [x] Daemon: launchd LaunchAgent `co.powerset.powerpacks-console.powerpacks-search-test`
      on port 5178 (`run|daemon-install|daemon-uninstall|daemon-status` in
      `scripts/run-powerpacks-console.sh`); keep-alive verified. Docker rejected
      (uv subprocesses + FDA + .powerpacks mounts).
- [x] Phase 1 My Contacts: `GET /local-api/contacts` mirrors prod
      `_parse_contact_search` (network-search-api contacts.py) — name/email:/
      phone:/headline:/company: + local twitter:/city:; ContactsV2-style table,
      URL-driven q/sort/dir/page. Verified: sample-user→4, email:@gmail.com→82,
      company:roblox→68, phone:→69.
- [x] Phase 2 Profile: `GET /local-api/contacts/person/:id` (profile+positions
      joined to local_companies+education+summary); PersonDetails-mirrored page
      at `/contacts/:personId`. Verified rich (25 pos) + sparse persons.
- [x] Phase 3 Local search: `POST /local-api/search/local-run` → reuses
      `local_search_pipeline.py prepare/run` via existing job system, writes a
      standard `search-network-*` run dir the existing viewer renders;
      `LocalSearchLauncher` box on runs view with per-step progress + spend note.
      No-LLM smoke test (bm25-only, --search-only): 274 rows in 2.8s, zero
      OpenAI/TurboPuffer/Postgres.
- Build + `tsc --noEmit` clean; only pre-existing python test failures.

Known follow-ups (small): run state `status` never flips to "completed"
(pre-existing CLI behavior, viewer-side); launcher navigates via full page load
(needs an `onNavigate` prop in app shell); real search spend = 1 expansion call
+ 1 embedding + LLM filter/rerank over candidates (flip `--search-only` in
`localSearch.ts` for retrieval-only).

---

# Powerpacks backlog 📋

> Created: 2026-06-11
>
> Change log:
> - 2026-06-11: Initial file; added relationship-strength search feature TODO.
> - 2026-06-11: Added index-hygiene skill TODO.
> - 2026-06-12: Added interaction-counts + identity-attribution plan (worktree
>   `interaction-counts`); corrected stale claim that
>   `local_person_source_summary` exists in the local index (it does not).

## Interaction counts + identity attribution 🩹

> Created: 2026-06-12 · worktree `interaction-counts`

### Root cause (verified on this machine, 2026-06-12)

- Interaction counts exist at **discover** for both channels — gmail
  `discover/gmail/contacts.csv` has `total_messages`/`thread_count`/
  `last_interaction`; messages `research_review.csv` has
  `imessage_message_count`/`whatsapp_message_count`/`last_message` — but die
  at **import** because `PEOPLE_SCHEMA_COLUMNS` has no interaction columns.
  Net: 0 of the 67 imessage-channel people in `merged/people.csv` carry any
  count; the local index has no interaction table; hydration's
  `local_interaction_counts()` probe (`hydrate_people.py:356`) finds nothing;
  `build_unified_profiles` hardcodes `total_interactions: None`
  (`packs/indexing/lib/people.py:709`).
- Matching is **name-only** (`network_match_method` histogram: all `name_*`).
  389 of 705 unmatched contacts failed for "missing contact name" — phone and
  email tiers would link them. Candidates come from the Powerset API cache
  only, not the local merged people.
- `research_review.csv` is the durable match store (the Jun 2 match results —
  81 matched / 63 suggested / 705 unmatched — survived a `contacts.csv`
  regeneration that blanked its match columns). The ~67-person count is the
  **approval gate working as designed** (`in_network=true` → 79 eligible →
  69 imported after dedupe), not data loss.

### Phase 1 — counts ride people.csv end-to-end ✅ implemented 2026-06-12

Design: two new schema columns instead of a sidecar/new table, so every
stage stays on the one shared schema.

- [x] **Schema** (`packs/ingestion/schemas/people_schema.py`): add
      `interaction_counts` (JSON object, channel→int, e.g.
      `{"gmail": 142, "imessage": 87}`) and `last_interaction` (ISO-8601 UTC
      string) to `PEOPLE_SCHEMA_COLUMNS`; add `interaction_counts` to
      `JSON_OBJECT_COLUMNS`. Normalize gmail's `YYYY-MM-DD HH:MM:SS+00:00`
      and messages' ISO-T timestamps to one format.
- [x] **Messages writer**
      (`discover_contacts_pipeline/messages.py::review_row_to_messages_people`):
      populate both columns from the review row's per-channel counts and
      last-message timestamps; drop the `messages_total=...` summary freetext
      hack (keep `selection=` reason). Channel-wise max in
      `merge_messages_people_candidate`.
- [x] **Gmail writer** (discover gmail contacts → people conversion feeding
      `enrich_people` / `gmail_people_csv`): carry `total_messages` →
      `{"gmail": n}` and `last_interaction`. Verify `enrich_people` round-trips
      the new columns (it rewrites rows via `normalize_people_row`, so the
      schema addition should be sufficient — confirm with test).
- [x] **Merge** (`merge_network_sources.py`): new interaction merge rule next
      to `LIST_VALUE_COLUMNS` — channel-wise **max** for `interaction_counts`
      (max, not sum, so re-merges stay idempotent; merge also re-consumes its
      own output as an input), max for `last_interaction`.
- [x] **Index**: `local_person_profiles` gains `interaction_counts` (JSON),
      `total_interactions` (int, sum of channel values), `last_interaction`;
      populate `total_interactions` in `build_unified_profiles` instead of
      hardcoded `None`.
- [x] **Hydration** (`hydrate_people.py`): read `total_interactions` from
      `local_person_profiles` (column-driven probe already exists; profiles
      satisfy `person_id` + `total_interactions` once the column lands).
- [x] **Tests**: schema round-trip, messages/gmail writer population, merge
      idempotency (re-merge twice → same counts), index build + hydration
      carry-through. Full suite via
      `uv run --project . python -m unittest discover -s tests`.
- [x] **Verification on this machine** (no enrichment, reuse existing
      rapidapi payloads): re-run messages materialize + merge + index build;
      assert the ~67 imessage people and gmail people carry counts in
      `local_person_profiles`; spot-check an agentic-SQL query
      ("most-messaged people") returns sane rows.

### Phase 2 — phone/email match tiers (Jake's fix) ✅ implemented 2026-06-12

- [x] **Approval gate (matching never expands the approved
      set).** `matched` auto-derives `in_network=true` downstream, so every
      tier is gated on `research_review.csv`: `matched` only for approved
      contacts; reviewed-but-unapproved contacts are skipped by identifier
      tiers entirely; all other hits (incl. name-tier matches against new
      local candidates) demote to `suggested` for review. The raw-contacts
      direct merge path attributes no interaction counts (no approval state).
      Verified: 80 matched ⊆ 81 approved, 0 outside.

- [x] Tier-0 **phone-exact** (E.164-normalized) and **email-exact** match
      before all name tiers in the contact matcher.
- [x] Candidate catalog: union the Powerset API cache with local merged
      people (`people.csv` `all_phones`/`all_emails`) so local-only people
      are matchable.
- [x] Target metric vs 81/63/705 baseline: 96 matched / 73 suggested /
      681 unmatched; 80/80 overlap agreement with the Jun-2 matched set,
      0 disagreements, 16 net-new; `phone_exact` is now the top method (76).
      Verification (temp sandbox, no enrichment, nothing written to
      `.powerpacks`): 69 messages people (34 with counts), 276/276 gmail
      people with counts, 290/500 merged people with counts incl. 21
      cross-channel, `local_person_profiles` carries totals, hydration
      returns `total_interactions` through `llm_profile_view`.
      Learned: the 391 "missing contact name" contacts stay unmatched
      because no source carries their phone↔identity mapping (gmail and
      LinkedIn people have no phones); fixing that bucket needs a
      phone-book source (e.g. macOS Contacts import), not a better
      matcher — noted under Phase 3 follow-ups.

### Phase 3 — heal skill + console identity view (later, separate PR)

- [ ] `$heal-contacts` skill: re-match with approval gate,
      `match_status=confirmed` idempotency.
- ~~Console Identity tab~~ (cancelled 2026-07-23: console app deleted).
- [ ] Small hardening: regeneration of `contacts.csv` should either preserve
      match columns or mark the review state stale — today the wipe is
      silent (cosmetic locally, but breaks any consumer reading
      `contacts.csv` match columns directly).

## Index hygiene skill 🧹

Build a dedicated skill (separate from search) for local index data quality,
powered by `local_duckdb_query`:

- [ ] duplicate-person detection (same name/linkedin slug, different ids)
- [ ] positions with missing/zero dates, impossible tenures
- [ ] company-resolution noise (one company_id absorbing unrelated people —
      e.g. the shared-`company_id` overlap blob found during agentic-SQL
      validation)
- [ ] coverage report: profiles vs positions vs summaries row alignment,
      empty enrichment columns (e.g. `company_stage` empty in current index)

Deliberately out of scope for `search-sql` / `search-network`.

## Relationship strength as a first-class search signal 🤝

Goal: let search filter/sort/rerank by how warm a contact actually is
("senior infra engineers I've actually talked to in the last year").

- [ ] **Hydration**: join `local_person_source_summary` during candidate
      hydration so each result carries `message_count`, `last_interaction`
      (most recent interaction date across channels), and `source_channels`.
- [ ] **Pipeline capture**: verify the ingestion pipeline actually captures
      last-interaction timestamps and message counts for every source
      (iMessage, WhatsApp, Gmail/msgvault, Twitter). If coverage is partial,
      either denormalize the fields onto the main people tables at index
      build time, or build a small `local_interactions` /
      `local_person_source_summary`-style aggregate table that all sources
      write into. (Per repo rules: no ledgers, no run ids — just another
      records JSONL + table in the existing index build.)
- [ ] **Filter DSL**: expose the new fields (`message_count`,
      `last_interaction_epoch`, `source_channels`) as filterable columns in
      the local filter DSL (`Gte` on recency, `Gt` on counts,
      `ContainsAny` on channels).
- [ ] **Rerankers**: update LLM filter/rerank prompts so they understand the
      relationship-strength fields and can use them when the query implies
      warmth ("people I know", "warm intro to ...").
- [ ] **Extraction**: teach query extraction to emit relationship-strength
      traits (e.g. "people I've messaged recently" → recency filter) so the
      signal is reachable from natural language, not just manual filters.

Context: `local_person_source_summary` does **not** exist in the local DuckDB
today (only the prod Postgres `person_source_summary` exists); the
interaction-counts plan above puts the signal on `local_person_profiles`
instead, which unblocks this item. Related new work:
the agentic SQL vertical (`search-sql` skill) can join it manually in the
meantime.
