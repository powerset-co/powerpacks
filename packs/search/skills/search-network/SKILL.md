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

## Local Happy Path

Uses the local DuckDB search index — no TurboPuffer, Postgres, or set
resolution. LLM filtering/reranking runs by default after local retrieval
(OpenAI only; the data path stays fully local). Use `--search-only` to skip
LLM stages entirely.

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

4. Show the preview compactly (it will include `scope: local_duckdb`). Ask
   exactly:

   `Execute this local search or modify it?`

5. If the user chooses `execute`, run the returned `execute_command` exactly.

6. Keep execution quiet until the command finishes.

### Agentic SQL fan-out (local mode only)

In parallel with steps 3–6, fan out to the `search-sql` skill
(`packs/search/skills/search-sql/SKILL.md`) via a sub-agent when either:

- the query has a relational or aggregate component the filter DSL cannot
  express — per-person aggregates ("2+ stints at startups"), career ordering
  ("engineer before PM"), person-to-person overlap ("worked with X at Y",
  "schoolmates of X"), or interaction history ("people I've messaged"); or
- the user explicitly asks for it ("also run the sql vertical", "sql:").

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
