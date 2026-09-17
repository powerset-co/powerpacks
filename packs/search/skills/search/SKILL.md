---
name: search
description: "The single people-search door for Powerpacks. You decide surface/backend/depth/mode and record it (decision.json): explicit words pick the backend (powerset uses TurboPuffer/Postgres; local uses DuckDB); a JD or job-posting URL runs the reviewed result-driven deep mode; company / relational-SQL / my-contacts requests go to their surfaces. Formerly $search-network."
---

# Search

The single entry point for people search. `$search` routes every query to the right surface, then
runs fast local/TurboPuffer retrieval itself for ordinary people searches.

Use this for any people search request:

- `$search software engineers in sf`
- `$search local: product managers in nyc`
- `$search https://jobs.lever.co/company/abc123`   ← deep JD → runs deep mode
- `$search senior engineers at series a fintech companies`
- `$search stanford engineers with 3-5 yoe in new york`
- `$search people who work at OpenAI`

> `$search` supersedes `$search-network` (the old name still works as an alias). The retrieval
> primitive is still `search_network_pipeline.py` — only the skill/route was renamed.

## How to run this skill

Deep JD searches keep the existing cheap filter, followed by original-evidence
Luna capability screening, not Gemma CE. Candidates rated 3+ receive parallel Terra qualification and Luna
opportunity judgments. Terra independently rechecks Luna opportunity cap 2;
overall is the lower of qualifications and the authoritative opportunity cap.
The viewer shows overall score and one explanation, sorted
by overall then capability. Ratings 1–2 show "Did not pass screen". Saved human
ratings remain separate. Ordinary non-JD searches keep their existing reranker.

The pipeline generates pond queries, filters, and traits; review them for
correctness, not extra specificity. Keep queries broad and positive and traits
terse. Correct extraction or location errors against the user's request/JD,
preserving intended breadth and OR alternatives. Do not add constraints, turn
preferences into requirements, or pad queries with exclusions. Correct wording
when needed; leave already-correct output alone. See `deep-mode.md` for review.

Before running, track these five steps in the harness's checklist:

    1. Decide + record the search decision (decision.json)
    2. Prepare the search (payload preview or deep query)
    3. Review — confirm requirements with the user
    4. Execute the search
    5. Present results

Work in order. A routed surface or deep mode owns steps 2–5 through its own skill.

## Step 1 — Decide the route (you are the router)

You make this decision — there is no classifier to run. A one-liner, a pasted JD, and a
job-posting URL all come through this same step and the same rules. Decide four things,
record them, and only then act.

<!-- decision-rules:start -->
Decide `surface`, `backend`, `depth`, and `mode` for the query:

1. **surface** — where the query belongs:
   - `people` — any search for people. The default when unsure.
   - `company` — the subject is companies (lookup / IDs / investors / funding / sector) and
     no people are asked for. "Engineers at companies backed by Sequoia" is `people`.
   - `sql` — the predicate needs cross-row or cross-person logic: per-person aggregates
     ("2+ startup stints"), role ordering ("engineers who became PMs"), or a join against
     another person ("overlapped with Jane at Stripe"). A person's name alone is NOT sql:
     "look up Jane Doe" and "who is Jane Doe" are `people` lookups. Common words like
     "career" or "worked with <a technology>" do not make a query sql.
   - `contacts` — "my contacts" / "set contacts" plus contact-field filtering.
2. **backend** — which index runs the search. The user's explicit words always win:
   - `powerset` — the user says "powerset", names a set, or says "team/shared network"
     (even if they also say "my network": "search my Powerset network" is `powerset`).
   - `local` — the user says "local", "offline", or "my imported network/contacts",
     even if remote credentials exist.
   - Unstated → environment default: if `POWERPACKS_LOCAL_SEARCH_DB` is set, or
     `.powerpacks/search-index/local-search.duckdb` exists with no TurboPuffer credentials
     configured, pick `local`; otherwise `powerset`. Both configured → `powerset`, and say
     which you picked in one line so the user can flip it.
   - Forced values: `sql` is always `local`; `company` and `contacts` are always `powerset`.
3. **depth** — how hard to search (people surface only):
   - `deep` — the input is a pasted JD or a job-posting URL, or the user asks for a
     deep/thorough/judged run or names the deliverable ("recruit ...", "build a shortlist",
     "source candidates"). Quality-superlative hiring intent also means deep when the request
     supplies a role/domain to judge: "best", "strongest",
     "most exceptional", "top-tier", or "cracked" candidates. A bare "find me candidates" with
     no role context remains fast/clarify; do not fabricate a hiring profile.
     A raw profile URL is not yet a supported deep-search intake: ask for the role/domain rather
     than claiming the internal shortlist-anchor expansion can start from that URL.
   - `fast` — everything else: one expansion → retrieval → rerank pass.
   - Deep uses the result-driven loop: one broad query, ordinary
     retrieval/filter/rerank, all retrieved results in the viewer,
     then one plain continue-or-done question; the model
     diagnoses and crafts each next query
     itself. Auto mode caps at four ponds; an explicit interactive request for another round
     is binding and can reopen a model-stopped run. Scores are display-only.
4. **mode** — how deep ponds are reviewed:
   - `interactive` — default. After each pond, open the results in the viewer and ask one
     plain question: another round, or done? Diagnosis and the next query are the model's job.
   - `auto` — only when the user explicitly says `auto` or `autonomous` in the request. Run the
     existing autonomous loop and review the completed search at the end.
   - Fast searches and non-people surfaces use `interactive`.
5. Uncertain on any axis → `people` / the environment default / `fast` / `interactive`, and state the
   uncertainty in `reason`. Never block on routing.
<!-- decision-rules:end -->

Record the decision before anything runs (checklist item 1). Create the run dir with a short
stable slug from the query (e.g. `swe-sf-stanford`) and write `decision.json`:

```json
{"surface": "people", "backend": "powerset", "depth": "fast", "mode": "interactive",
 "reason": "<one sentence on why>"}
```

- fast → `.powerpacks/search/<slug>/decision.json`, and pass the same dir as `--output-dir`
  to `prepare` so the decision, payload, and outputs live together.
- deep → `.powerpacks/deep-search/<jd-slug>/decision.json` (the engine's existing run dir).

Then dispatch — this table is the whole routing contract:

| decision | action |
|---|---|
| surface `company` | load `packs/search/skills/search-company/SKILL.md` (decision.json still written first) |
| surface `sql` | load `packs/search/skills/search-sql/SKILL.md` (decision.json still written first) |
| surface `contacts` | load `packs/contacts/skills/search-contacts/SKILL.md` (decision.json still written first) |
| `people` + `fast` + `local` | **Local Happy Path** below (`search_network_pipeline.py prepare --backend local --db <db>`) |
| `people` + `fast` + `powerset` | **TurboPuffer Happy Path** below (`search_network_pipeline.py prepare`) |
| `people` + `deep` | load `packs/search/skills/search/deep-mode.md` (`--jd-file` / `--jd-url` as it documents; on backend `local` add `--backend local --db <db>` to `deep_search_loop.py`) |

The deep engine owns orchestration and delegates each reviewed pond to the
ordinary `search_network_pipeline.py prepare/run` path. Follow `deep-mode.md` so query,
compiled traits/filters, result deltas, diagnosis, and the one next move stay in the fixed
search-harness artifact. There is no other deep engine.

Input shapes normalize before `prepare`, never before the decision:

- **backend directives are directives, not query text** — strip words like `local:`, "offline",
  "in powerset", "search powerset for", or a set name from the text you pass as `--query`; they
  bound the decision, and leaving them in pollutes query expansion.

- **job-posting URL** — deep mode fetches it itself (`--jd-url`). Only when the user
  explicitly forces `fast` on a URL, fetch first with
  `uv run --project . python packs/search/primitives/deep_search/fetch_jd.py --url <url> --out <run>/jd.txt`
  (a thin fetch under ~400 chars → ask for a paste; Ashby URLs resolve via the public
  posting API automatically) and use the fetched text as the query.
- **pasted JD forced to `fast`** — use the JD text directly as `--query`; expansion condenses it.
- **one-liner** — the query as-is.

**The spend gate (checklist item 3):** fast mode confirms the prepare preview once
(`Execute this search or modify it?`, or the local path's `Execute this local search or modify
it?`). Deep mode confirms its initial query and filters once — the only approval in
the flow. Interactive deep mode pauses after each pond only to ask continue-or-done at the
viewer; auto deep mode runs all approved ponds without that pause.

### Retrieval surface boundary

`$search` people retrieval means the Powerpacks network surface: the `powerset` backend is
set-scoped TurboPuffer/Postgres and the `local` backend is DuckDB. It is not Sales Navigator.

- An explicit Powerset/network request stays on `$search` with `surface: people` and
  `backend: powerset`, including retries, wider probes, adjacency, and sparse-result diagnosis.
- Never treat Sales Nav or LinkedIn leads as an implicit fallback for a failed or weak `$search`
  run. Ask before changing retrieval surfaces and keep artifacts/results separate unless the user
  explicitly requests both.
- If the user says only "extended search" and the conversation has not defined the surface, ask:
  `Do you mean Sales Nav extended search, or regular Powerset network search?`

---

## Hiring seniority & hireability defaults

These apply to every hiring-intent search (a JD, a role brief, "find
candidates", "people like X for this role") in both local and TurboPuffer
modes, and they bind any fallback behavior too:

The order is **explicit user preferences > JD-supported inference > defaults**.
Apply these when reviewing the query and ordinary compiled payload. Defaults rank;
they do not silently become JD hard requirements. The user can override them at review.

- **Derive the seniority target from level language, else use the general IC
  range.** Map stated levels ("senior", "staff+", "lead", "head of") to
  seniority bands. A candidate role with no explicit level uses
  junior/mid/senior/staff. Never derive bands from years of
  experience, team size, scope, or impact language — YOE is unreliable
  ("8+ years" does not mean senior). Preserve extractor-inferred bands
  unless they contradict the query.
- **Explicit leadership language overrides the IC default.** Lead, head,
  manager, director, VP, and C-suite searches use the corresponding bands;
  never append negative title clauses to approximate seniority.
- **"People like <person>"** anchors seniority to that person's current
  role and band (same rule as the deep-search engine). If the anchor is still
  ambiguous, ask exactly one question before executing: "Hands-on IC
  engineers only, or are technical leaders (VP/director/CTO) acceptable
  if still hands-on?"
- **Preserve the user's stated constraints exactly; never add exclusions.** When the
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

Uses the local DuckDB search index - no TurboPuffer, Postgres, or set
resolution. Retrieval stays local, but LLM filtering/reranking runs by default
and sends the required candidate evidence to the configured OpenAI boundary.
Use `--search-only` to skip those model stages entirely.

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
worked with John Doe"), it is not this fast path; follow the Step 1 route.

1. Determine the DuckDB path:
   - `$POWERPACKS_LOCAL_SEARCH_DB` if set
   - Otherwise `.powerpacks/search-index/local-search.duckdb`

2. If the DB file does not exist, tell the user to run
   `$build-local-search-index` first and stop.

3. Run:

   ```bash
   uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
     --backend local \
     --query "<user query>" \
     --db "<db-path>" \
     --output-dir ".powerpacks/search/<slug>"
   ```

   Use the same `<slug>` run dir where `decision.json` was recorded.

4. Show the query without an agent-written targeting or filter summary.
   Review the compiled filters for correctness: a role noun like "product
   managers" must not silently become a `manager` seniority band.
   If `runtime_notes` flags a broad search
   (hard filters match more than ~60% of the index), surface that note and
   recommend narrowing before executing — running LLM stages over most of
   the index is usually a query problem, not a retrieval problem. If it
   flags 0 matches or a suspiciously narrow pool, recommend `modify`. Then ask exactly:

   `Execute this local search or modify it?`

5. If the user chooses `execute`, run the returned `execute_command` exactly.

6. Keep execution quiet until the command finishes.

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
     --query "<user query>" \
     --output-dir ".powerpacks/search/<slug>"
   ```

   Use the same `<slug>` run dir where `decision.json` was recorded. (Deep-engine delegated
   profile searches pass their own output dir; follow the engine's instructions there.)

2. If `prepare` returns `status: company_directory_fast_path`, follow the
   returned tool request and skip semantic retrieval.
3. If `prepare` returns a preview, show the query without an agent-written
   targeting or filter summary. Then ask exactly:

   `Execute this search or modify it?`

4. If the user chooses `execute`, run the returned `execute_command` exactly.
   It already includes `--execute-approved`; do not ask for another approval.
   - If a **limit** was provided (e.g. by the deep-search engine for a
     capped profile search), append `--limit <N>` to the execute_command (or
     pass `--limit` to `prepare`, which threads it through). This caps
     retrieval and the whole downstream pipeline. For standalone user
     searches, do not add a limit unless the user asks for one.
   - If **filter-only mode** was requested (profile searches do this), append
     `--filter-only`. The run keeps the cheap conservative LLM filter but
     skips the expensive per-search LLM rerank; final ranking is owned by the
     caller's evaluation pass. Never use `--filter-only` for standalone user
     searches — they need the rerank for good ordering.
5. Keep execution quiet until the command finishes or emits a concrete
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

## User edit & feedback capture

Log each user query/filter/pond edit or result note immediately:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py log \
  --run-dir <run> --kind <filter_edit|query_edit|pond_edit|result_feedback> \
  --note "<one line in the user's words>" [--before "<old value>"] [--after "<new value>"]
```

Use identifiers only, never message content. At the end of a run with edits,
send once:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py send \
  --run-dir <run>
```

`needs_auth` is normal; keep the local log and do not request login. Concrete
person-data errors still use `$feedback`.

## Execution Rules

- Never spend before the checklist-item-3 confirmation. In interactive deep mode, also wait for
  the required pond query/payload review; in auto deep mode, the approved query authorizes the loop.
- Do not run doctor or setup checks before a normal search unless the primitive
  fails with an unclear auth/env/setup error.
- Do not use sub-agents for ordinary single-query searches.
- Do not write new retrieval scripts during a search run.
- Do not filter or reuse prior artifacts for refinements; create a new search
  with the updated query or constraints.
- Do not mention skip-rerank, alternate execution modes, internal ledgers, or
  internal artifact paths in the user-facing preview.

The packaged primitives own extraction, resolution, filtering, reranking, and
persistence. Treat their output as authoritative and inspect internals only
after a failed or inconsistent run, or when the user asks to debug.
