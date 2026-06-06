---
name: search-network
description: Run a Powerpacks people search from a natural-language query such as role, title, company, location, school, investor, or domain criteria. Use for ordinary people searches and company-people lookups. If the input is a job posting URL, pasted job description, or complex role brief, load search-network-jd instead.
---

# Search Network

Use this for ordinary people search requests such as:

- `/search-network software engineers in sf`
- `/search-network senior engineers at series a fintech companies`
- `/search-network stanford engineers with 3-5 yoe in new york`
- `/search-network people who work at OpenAI`

## Happy Path

Do not inspect repo docs, source, memory, prior transcripts, or prior result
files on the happy path. Start a fresh run for every search request.

1. If the input is a job posting URL, pasted JD, or broad multi-trait role
   brief, use the installed `search-network-jd` skill instead.
2. Otherwise run:

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
