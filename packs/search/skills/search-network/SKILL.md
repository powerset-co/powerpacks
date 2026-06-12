---
name: search-network
description: "Run a Powerpacks people search from a natural-language query, job description URL, or pasted JD. Routes automatically between local DuckDB search (your imported network), TurboPuffer remote search (non-personal set / team network), and multi-probe JD workflows based on the input and environment."
---

# Search Network

Use this for any people search request:

- `$search-network software engineers in sf`
- `$search-network local: product managers in nyc`
- `$search-network https://jobs.lever.co/company/abc123`
- `$search-network senior engineers at series a fintech companies`
- `$search-network stanford engineers with 3-5 yoe in new york`
- `$search-network people who work at OpenAI`

## Mode Detection

Apply these rules in order:

1. **Profile mode** — if the input is a URL pointing to a job posting, a
   pasted multi-paragraph job description, a broad multi-trait role brief
   that needs multiple distinct candidate profiles, **or a similar-person
   request** ("find me more people like <linkedin url>", "people similar to
   X" with a LinkedIn profile URL), load and follow the **`search-profile`**
   skill entirely. Read its instructions from the installed skill directory
   at `../search-profile/SKILL.md` (relative to this skill) or from the repo
   at `packs/search/skills/search-profile/SKILL.md`. Do not run
   `search_network_pipeline.py` directly for these inputs — the profile skill
   owns the orchestration (including resolving the person's profile for
   similar-person requests) and delegates individual profile searches back
   here (TurboPuffer mode) with a per-search `limit` and filter-only flag.

2. **Local mode** — if any of these are true:
   - The user says "local", "local search", "offline", or "my imported
     network" / "my network" **without** mentioning Powerset, a set name, or
     the team network
   - `POWERPACKS_LOCAL_SEARCH_DB` is set in the environment or `.env`
   - `.powerpacks/search-index/local-search.duckdb` exists and no TurboPuffer
     credentials are configured
   Then use the **Local Happy Path** below.

3. **TurboPuffer mode** — everything else. This is the default for queries
   against a Powerset set, team network, or any remote-backed search.
   Use the **TurboPuffer Happy Path** below.

Disambiguation:

- Any mention of "Powerset", a set name/ID, or the team/shared network always
  means **TurboPuffer**, even if the user also says "my network" (e.g.
  "search my Powerset network" is TurboPuffer, not local).
- "Local", "offline", or "my imported contacts" always means **Local**, even
  if remote credentials exist.
- When both local DB and TurboPuffer creds exist and the user didn't specify
  either way, prefer TurboPuffer.

---

## Hiring seniority & recruitability defaults

These apply to every hiring-intent search (a JD, a role brief, "find
candidates", "people like X for this role") in both local and TurboPuffer
modes, and they bind any fallback behavior too:

- **Derive the seniority target from explicit level language only.** Map
  stated levels ("senior", "staff+", "director and above") to seniority
  bands. Never derive bands from years of experience, team size, scope, or
  impact language — YOE is unreliable ("8+ years" does not mean senior), and
  level-less IC titles like "Member of Technical Staff" derive NO band. If
  the query/JD has no explicit level, leave bands empty and surface it in
  the preview (`Targeting: all levels — pin a band?`). Preserve
  extractor-inferred bands unless they contradict the query.
- **Exclude current founders / co-founders / CEOs / C-suite by default**
  for role searches. They are rarely recruitable for an IC or leadership
  hire. State the default in the preview (one line such as
  `Excluding current founders/C-suite — say "include founders" to keep
  them`) so the user can flip it. Include them only when the user
  explicitly asks for founder-type profiles or "builders regardless of
  current title".
- **Never silently exclude VP / director / manager / head.** Some are
  hands-on and appropriate depending on company stage. Keep them unless
  the user excludes them; the rerank judges hands-on fit.
- **"People like <person>"** anchors seniority to that person's current
  role and band (same rule as `search-profile`). If the anchor is still
  ambiguous, ask exactly one question before executing: "Hands-on IC
  engineers only, or are technical leaders (VP/director/CTO) acceptable
  if still hands-on?"
- **Preserve the user's stated constraints exactly; never add hidden
  exclusions beyond the founder default above without asking.** When the
  user corrects a seniority interpretation, that correction binds every
  subsequent search in the session — repeating a corrected mistake is the
  worst outcome.
- **On pipeline failure, do not improvise retrieval.** Report the failure
  (the "do not write new retrieval scripts" rule still holds). If the
  user explicitly asks for a manual fallback over the local index, the
  fallback must apply these same seniority defaults — in particular,
  never put founder/CEO/CTO into a technical-title pattern by default.

---

## Local Happy Path

Uses the local DuckDB search index — no TurboPuffer, Postgres, or set
resolution. LLM filtering/reranking runs by default after local retrieval
(OpenAI only; the data path stays fully local). Use `--search-only` to skip
LLM stages entirely.

### Local person lookup fast path

If the query is a bare person identifier with no role/filter intent — a
name ("John Doe", "who is John Doe"), an email, a phone number, a Twitter/X
handle, or a LinkedIn profile URL — do **not** run the pipeline. Names and
identifiers are not indexed by any retrieval stage; run one direct lookup
instead:

```bash
uv run --project . python packs/search/primitives/local_duckdb_query/local_duckdb_query.py query \
  --sql "SELECT person_id, full_name, headline, current_title, current_company, city, linkedin_url FROM local_person_profiles WHERE full_name ILIKE '%john doe%'"
```

Match emails against `primary_email`/`all_emails`, phones against
`primary_phone`/`all_phones`, handles against
`twitter_handle`/`x_twitter_handle`, LinkedIn URLs against
`linkedin_url`/`public_identifier` (normalize to the slug). Show the
matches compactly; if several people match, list them all. If zero match,
say so and offer a normal search. Skip extraction, task state, retrieval,
hydration, and all LLM stages — this is a deterministic lookup, not a
search. If the query combines a person with anything else ("engineers who
worked with John Doe"), it is not this fast path — use the normal flow and
the agentic SQL fan-out gate.

1. Determine the DuckDB path:
   - `$POWERPACKS_LOCAL_SEARCH_DB` if set
   - Otherwise `.powerpacks/search-index/local-search.duckdb`

2. If the DB file does not exist, tell the user to run
   `$build-local-search-index` first and stop.

3. Run:

   ```bash
   uv run --env-file .env --project . python packs/search/primitives/local_search_pipeline/local_search_pipeline.py prepare \
     --query "<user query>" \
     --db "<db-path>"
   ```

4. Show the preview compactly (it will include `scope: local_duckdb` and a
   `pool_estimate` with `matched_people` / `total_people`). Include one line
   like `Pool: 150 of 500 people`. If `runtime_notes` flags a broad search
   (hard filters match more than ~60% of the index), surface that note and
   recommend narrowing before executing — running LLM stages over most of
   the index is usually a query problem, not a retrieval problem. If it
   flags 0 matches, recommend `modify` (or expect the zero-result fallback
   below). Then ask exactly:

   `Execute this local search or modify it?`

5. If the user chooses `execute`, run the returned `execute_command` exactly.

6. Keep execution quiet until the command finishes.

### Agentic SQL fan-out (local mode only)

In parallel with steps 3–6, fan out to the `search-sql` skill
(`packs/search/skills/search-sql/SKILL.md`) via a sub-agent — but only when
the gate below passes. Default is OFF; most searches must not fan out.

Decision test: **could the need be expressed as filters over one position
row at a time?** If yes, do not fan out — the main retrieval stages own it.
Fan out only when the query needs one of:

- **counting/aggregation across a person's rows** — "2+ stints at
  startups", "average tenure under 2 years", "worked at 3+ FAANG companies"
- **ordering/sequence between a person's roles** — "engineers who became
  product managers", "promoted internally", "IC before manager"
- **a join against another person** — "worked with X", "overlapped with X
  at Y", "schoolmates of X", "people similar to X's career path"
- **set algebra over two sub-populations** — "ex-Stripe folks now at infra
  startups"
- **cross-trait evidence living on different rows or tables** — "designers
  who can code" (the design role is one position row; the coding evidence
  is a different engineering row or `local_summaries.tech_skills`),
  "recruiters with a technical background", "founders who were previously
  sales". One position row cannot satisfy both traits, so per-row filters
  cannot express the conjunction — still run hybrid in parallel, since
  profile prose sometimes carries both signals.
- **interaction history** — "people I've actually messaged" (requires
  `local_person_source_summary`; skip if the table is absent)
- **explicit user request** — "also run the sql vertical", "sql:"

Never fan out for role/title/seniority/location/company/education/date
filters, however many are combined — "senior Stanford engineers at series A
fintechs in NYC since 2020" is still one-row-at-a-time and stays in the
main path. When unsure, do not fan out; the user can ask for `sql:` on a
follow-up.

Give the sub-agent the user query verbatim plus any already-resolved person
or company ids, and have it follow `search-sql`'s output contract. Do not
fan out for plain row-level searches — the main retrieval stages own those.

Fan-in goes through the pipeline, not around it:

1. Run `prepare` and fan out the sub-agent while the user reviews the
   preview.
2. Write the sub-agent's output JSON to `agentic-sql-candidates.json` inside
   the run's output directory.
3. Append `--extra-candidates-json <that path>` to the returned
   `execute_command` before running it. The pipeline unions the SQL people
   into retrieval (tagged `agentic_sql` in `vertical_sources`), so they flow
   through the **same** `hydrate_people`, `llm_filter_candidates`, and
   `llm_rerank_candidates` steps as every other candidate — no separate
   ranking path.
4. If the sub-agent has not finished by the time the user approves
   execution, wait briefly for it; if it fails or returns an empty `people`
   list, run the `execute_command` without the flag and note the vertical
   was skipped. The SQL vertical is additive evidence — never block or fail
   the search on it.

### Zero-result SQL fallback (local mode only)

If the pipeline completes with 0 found (or the preview's `pool_estimate`
already shows 0 matched), fan out one `search-sql` sub-agent with the user
query **and** the payload's `role_search_filters`, asking it to:

1. probe the actual value spaces of each hard-filtered column,
2. identify which constraint zeroed the pool (e.g. a filter value that does
   not exist in the index taxonomy),
3. return candidates matching the user's intent with corrected values, in
   the standard output contract.

Present the diagnosis in one line ("`seniority_bands: [manager]` matched 0
because this index uses ..."), plus the recovered candidates if any. Offer
to re-run the proper pipeline with corrected filters; do not silently
substitute SQL results for a full search.

### Local Summary

- Say `<N> found (local)`.
- Say `Run artifacts: <artifact-dir>`.
- Show top 10 candidates from the CSV: rank, name, current title/company,
  location, LinkedIn URL when present.
- If the SQL fan-out ran, say `<M> sql-vertical candidates merged` (read
  `agentic_sql_tagged` from the execute_role_search step summary). SQL-only
  people appear in the main ranked CSV like everyone else; their rows carry
  `agentic_sql` in `vertical_sources`.

### Local Constraints

- LLM filter/rerank run by default and need `OPENAI_API_KEY`; if it is
  missing, rerun with `--search-only` instead of failing the search
- No set/operator resolution
- No TurboPuffer or Postgres calls
- Investor filters are not supported locally

---

## TurboPuffer Happy Path

Do not inspect repo docs, source, memory, prior transcripts, or prior result
files on the happy path. Start a fresh run for every search request.

1. Run:

   ```bash
   uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
     --query "<user query>"
   ```

3. If `prepare` returns `status: company_directory_fast_path`, follow the
   returned tool request and skip semantic retrieval.
4. If `prepare` returns a preview, show it compactly. When the extracted
   filters include `seniority_bands` (or the query names a band), include one
   compact line such as `Targeting: senior/staff ICs` so the user can correct
   the band before executing. If there is no seniority target, omit the line
   — do not invent one. Then ask exactly:

   `Execute this search or modify it?`

5. If the user chooses `execute`, run the returned `execute_command` exactly.
   It already includes `--execute-approved`; do not ask for another approval.
   - If a **limit** was provided (e.g. by the `search-profile` skill for a
     capped profile search), append `--limit <N>` to the execute_command (or
     pass `--limit` to `prepare`, which threads it through). This caps
     retrieval and the whole downstream pipeline. For standalone user
     searches, do not add a limit unless the user asks for one.
   - If **filter-only mode** was requested (profile searches do this), append
     `--filter-only`. The run keeps the cheap conservative LLM filter but
     skips the expensive per-search LLM rerank; final ranking is owned by the
     caller's evaluation pass. Never use `--filter-only` for standalone user
     searches — they need the rerank for good ordering.
6. Keep execution quiet until the command finishes or emits a concrete
   `blocked_approval` / `blocked_user_action`.

## Final Summary

- Say `<N> found`.
- Say `Run artifacts: <artifact-dir>`.
- Read only the `csv` path from the final `artifacts` object and show the top
  10 candidates, or fewer if fewer than 10 rows were returned. Keep each row
  compact: rank, name, current title/company, location, and LinkedIn URL when
  present.
- Other run files are internal handoff/debug artifacts. Inspect them only for a
  failed or inconsistent run, or when the user asks to debug.

## Execution Rules

- Do not run doctor or setup checks before a normal search unless the primitive
  fails with an unclear auth/env/setup error.
- Do not use sub-agents for ordinary single-query searches. (Exception: the
  local-mode agentic SQL fan-out above, only when its trigger conditions are
  met.)
- Do not write new retrieval scripts during a search run.
- Do not filter or reuse prior artifacts for refinements; create a new search
  with the updated query or constraints.
- Do not mention skip-rerank, alternate execution modes, internal ledgers, or
  internal artifact paths in the user-facing preview.

## Primitive-Owned Behavior

The packaged primitives own extraction, company-only detection, company and set
resolution, structured traits, hard-filter/filter-only handling, LLM filtering,
reranking, and persistence. Treat primitive output as the source of truth.

The neighboring `network-search-api` is the reference implementation for newer
search behavior, including structured traits (`value`, `temporal`, `meaning`),
grouped scoring for hard-filter-backed traits, filter-only fallback for
hard-filter-only queries, and rerank skip behavior when no traits/candidates are
available. Do not reimplement those behaviors in the skill; port or update the
packaged primitives when behavior needs to change.

## Debugging

Use internals only after a blocker, failed run, inconsistent final summary, or
explicit user request. Useful internal surfaces include the task state, ledger,
hydration outputs, rerank handoff files, manifest, and primitive source.

For the old manual normal-search orchestration notes, see:
`packs/search/skills/search-network-legacy-normal-strategy-loop.txt`.
