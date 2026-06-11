# Console: My Contacts + Local Search plan 🗺️

Created: 2026-06-11

## Change log
- 2026-06-11: Initial plan (investigation of app/ + network-search-app + DuckDB schema).
- 2026-06-11: Phases 1–3 implemented via parallel agent team + launchd daemon + plugin refactor. See Review.

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
Local additions Arthur asked for: `twitter:` (bare = has handle), plus cheap
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
      URL-driven q/sort/dir/page. Verified: arthur→4, email:@gmail.com→82,
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

Context: today `local_person_source_summary` exists in the local DuckDB but
no retrieval stage, hydration step, or reranker reads it. Related new work:
the agentic SQL vertical (`search-sql` skill) can join it manually in the
meantime.
