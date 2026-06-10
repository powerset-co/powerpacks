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
   pasted multi-paragraph job description, or a broad multi-trait role brief
   that needs multiple distinct candidate profiles, load and follow the
   **`search-profile`** skill entirely. Read its instructions from the
   installed skill directory at `../search-profile/SKILL.md` (relative to
   this skill) or from the repo at
   `packs/search/skills/search-profile/SKILL.md`. Do not run
   `search_network_pipeline.py` directly for these inputs — the profile skill
   owns the orchestration and delegates individual profile searches back here
   (TurboPuffer mode) with a per-search `limit` and filter-only flag.

2. **Local mode** — if any of these are true:
   - The user says "local", "my network", "local search", or "offline"
   - `POWERPACKS_LOCAL_SEARCH_DB` is set in the environment or `.env`
   - `.powerpacks/search-index/local-search.duckdb` exists and no TurboPuffer
     credentials are configured
   Then use the **Local Happy Path** below.

3. **TurboPuffer mode** — everything else. This is the default for queries
   against a Powerset set, team network, or any remote-backed search.
   Use the **TurboPuffer Happy Path** below.

When both local DB and TurboPuffer creds exist and the user didn't specify,
prefer TurboPuffer. If the user explicitly asks for local, use local.

---

## Local Happy Path

Uses the local DuckDB search index — no TurboPuffer, Postgres, set resolution,
or LLM rerank calls.

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

### Local Summary

- Say `<N> found (local)`.
- Say `Run artifacts: <artifact-dir>`.
- Show top 10 candidates from the CSV: rank, name, current title/company,
  location, LinkedIn URL when present.

### Local Constraints

- No LLM filtering or reranking (local pipeline is search-only by design)
- No set/operator resolution
- No TurboPuffer or Postgres calls
- Investor filters, interaction metrics are not supported locally

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
4. If `prepare` returns a preview, show it compactly and ask exactly:

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
- Do not use sub-agents for ordinary single-query searches.
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
