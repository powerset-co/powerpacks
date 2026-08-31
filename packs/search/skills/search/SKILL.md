---
name: search
description: "The single people-search door for Powerpacks. You decide surface/backend/depth/mode and record it (decision.json): explicit words pick the backend (powerset uses TurboPuffer/Postgres; local uses DuckDB); a JD or job-posting URL runs the reviewed result-driven deep mode; company / relational-SQL / my-contacts requests go to their surfaces. Formerly $search-network."
---

<!--
Changelog:
- 2026-08-31: Every user edit (filters, queries, ponds) and every result feedback is logged
  to the run dir via search_feedback.py, and one aggregated row is sent to the Powerset
  feedback endpoint at the end of the run (needs_auth is a normal quiet outcome).
- 2026-08-24: Deep mode reviews each pond with the user by default. An explicit `auto`
  request opts into autonomous ponds, and every completed loop opens its run-scoped results.
- 2026-08-22: Deep mode runs the result-driven search harness: editable query and payload,
  review of every reranked candidate scoring at least 0.70, falling back to at least 0.30
  only when none clear 0.70, autonomous diagnosis and one next move, and at most four ponds
  after the user approves the plan.
- 2026-08-22: Deep mode retrieves prior human edits for Terra payload proposals and next-move
  context, logging proposal deltas; company context is RapidAPI-only and display-only.
- 2026-08-17: Deep mode defaults to one reviewed plan plus exactly five editable query arms. Each
  arm runs the ordinary search pipeline with shared evaluation settings; the legacy convergence
  engine is opt-in as `--mode exhaustive`.
- 2026-07-08: Checklist step 3 is now "Review — confirm requirements with the user" (was "GATE —").
  Plain "Review" instead of "GATE"/"GATE 1" in the printed tasks; the core-gate keeps its name.
- 2026-07-01: Replaced the Step-0 classifier (route_query.py, deleted) with an agent-made decision
  contract — you decide surface/backend/depth and record decision.json before anything runs. Added
  the mandatory native-task checklist and the universal confirm-before-execute gate. Explicit
  "powerset"/"local" words now bind the backend end-to-end. Decision quality is benchmarked by the
  agent decision eval (packs/search/evals/run_decision_eval.py) instead of the offline classifier eval.
- 2026-06-30: Renamed from `search-network` to `search` (search consolidation Stage 3). Added the
  Step-0 router that dispatches deep JD/URL/brief/shortlist to $search's deep mode and
  company/sql/contacts to their surfaces; ordinary people searches stay on the fast local/TurboPuffer
  path. $search-network is a deprecated alias. The retrieval primitive (search_network_pipeline.py)
  and search-network-jd-* schemas/tasks keep their names.
- 2026-07-10: Quality-superlative hiring asks ("best", "strongest", "cracked") enter deep mode.
  Deep mode now builds and critiques its recruiter plan before sourcing, then uses the approved
  core/nice criteria and explicit recruiter defaults to generate epoch-0 probes.
-->

# Search

The single entry point for people search. `$search` routes every query to the right surface, then
runs fast local/TurboPuffer retrieval itself for ordinary people searches.

For the product and system walkthrough, read
[`packs/search/docs/search-architecture.md`](../../docs/search-architecture.md).
This file remains the executable agent contract.

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

**FIRST, before running anything: create a literal, visible checklist with the five steps
below and step through it, marking each item complete as you go.** This is mandatory. Use
your harness's plan/todo/task tool:

- **Claude Code:** `TaskCreate` one task per step (1–5), then `TaskUpdate` each to
  `in_progress` then `completed` as you go.
- **Codex:** `update_plan` with the five steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

Seed the checklist with these exact item titles:

    1. Decide + record the search decision (decision.json)
    2. Prepare the search (payload preview or deep plan)
    3. Review — confirm requirements with the user
    4. Execute the search
    5. Present results

Work the checklist in order 1 → 5. Exactly one item `in_progress` at a time; mark it
`completed` before starting the next. No batching, no reordering, no skipping, no invented
extra steps. If Step 1 decides surface `company`/`sql`/`contacts`, mark items 2–5 as handed
off and load that surface's SKILL — it owns its own flow. If Step 1 decides depth `deep`,
items 2–5 are owned by deep mode's own checklist (`deep-mode.md`) — load it right after
recording the decision.

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
     retrieval/filter/rerank, all results scoring at least 0.70 in the viewer (or at least
     0.30 when none clear 0.70), then one plain continue-or-done question; the model
     diagnoses and crafts each next query
     itself. Auto mode caps at four ponds; an explicit interactive request for another round
     is binding and can reopen a model-stopped run. Scores are display-only; the prior judge/consensus/anchor
     convergence engine is explicit `--mode exhaustive` only.
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
search-harness artifact. Use `--mode exhaustive` only when explicitly requested.

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
it?`). Deep mode confirms the plan plus its one or two initial queries once — the only approval in
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

Deep mode resolves these through the versioned recruiter policy at
`packs/search/policies/recruiter-defaults.json` and embeds the resolved values plus provenance in
`epoch0/plan.json`. The order is **explicit user preferences > JD-supported inference > defaults**.
Defaults rank; they do not silently become JD hard requirements. Review shows them once so the
user can override them before sourcing.

- **Derive the seniority target from level language, else from the title's
  conventional range.** Map stated levels ("senior", "staff+", "director and
  above") to seniority bands. When a hiring JD/title states no explicit
  level, propose the title's conventional band range (e.g. "Member of
  Technical Staff" or a bare "Software Engineer" → mid/senior — MTS is not
  the `staff` band despite the word) and show it in the preview's
  `Targeting:` line for correction. Never derive bands from years of
  experience, team size, scope, or impact language — YOE is unreliable
  ("8+ years" does not mean senior). Preserve extractor-inferred bands
  unless they contradict the query.
- **Exclude current founders / co-founders / CEOs / C-suite by default**
  for role searches. They are rarely hireable for an IC or leadership
  hire. State the default in the preview (one line such as
  `Excluding current founders/C-suite — say "include founders" to keep
  them`) so the user can flip it. Include them only when the user
  explicitly asks for founder-type profiles or "builders regardless of
  current title".
- **Never silently exclude VP / director / manager / head.** Some are
  hands-on and appropriate depending on company stage. Keep them unless
  the user excludes them; the rerank judges hands-on fit.
- **"People like <person>"** anchors seniority to that person's current
  role and band (same rule as the deep-search engine). If the anchor is still
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
worked with John Doe"), it is not this fast path — use the normal flow and
the agentic SQL fan-out gate.

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

4. Show the preview compactly (it will include `scope: local_duckdb` and a
   `pool_estimate` with `matched_people` / `total_people`). Include one line
   like `Pool: 150 of 500 people`. When the extracted filters include
   `seniority_bands` (or the query names a band), include one compact line
   such as `Targeting: senior/staff ICs` so the user can correct the band
   before executing — a role noun like "product managers" must not silently
   become a `manager` seniority band. If `runtime_notes` flags a broad search
   (hard filters match more than ~60% of the index), surface that note and
   recommend narrowing before executing — running LLM stages over most of
   the index is usually a query problem, not a retrieval problem. If it
   flags 0 matches or a suspiciously narrow pool, recommend `modify` (or
   expect the zero-result fallback below). Then ask exactly:

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
     --query "<user query>" \
     --output-dir ".powerpacks/search/<slug>"
   ```

   Use the same `<slug>` run dir where `decision.json` was recorded. (Deep-engine delegated
   profile searches pass their own output dir; follow the engine's instructions there.)

2. If `prepare` returns `status: company_directory_fast_path`, follow the
   returned tool request and skip semantic retrieval.
3. If `prepare` returns a preview, show it compactly. When the extracted
   filters include `seniority_bands` (or the query names a band), include one
   compact line such as `Targeting: senior/staff ICs` so the user can correct
   the band before executing. If there is no seniority target, omit the line
   — do not invent one. Then ask exactly:

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

This applies to every `$search` run, fast and deep (the run dir is
`.powerpacks/search/<slug>` or `.powerpacks/deep-search/<jd-slug>`).

**Log every user change the moment it happens.** Whenever the user modifies
anything about the search — changes the query wording, drops/adds/corrects a
filter or seniority band at the `modify` gate, flips a default (e.g. "include
founders"), edits a pond query or payload — or gives any feedback about the
results ("wrong person", "this ranking is off", "top result is stale"), run:

```bash
uv run --project . python packs/search/primitives/search_feedback/search_feedback.py log \
  --run-dir <run> --kind <filter_edit|query_edit|pond_edit|result_feedback> \
  --note "<one line in the user's words>" [--before "<old value>"] [--after "<new value>"]
```

It appends to `<run>/user-edits.jsonl`. Identifiers only (names, LinkedIn
URLs, queries, filter values) — never message content.

**Send once per run, at the end.** After the final summary (or at the end of
the search turn, whichever comes last), if anything was logged, run:

```bash
uv run --project . python packs/search/primitives/search_feedback/search_feedback.py send \
  --run-dir <run>
```

One aggregated row goes to the Powerset feedback endpoint — one row per run
no matter how many edits. `status: needs_auth` (not logged in) is a normal
outcome: the local log is the record, say nothing beyond one line, and do not
ask the user to log in or retry. A successful send rotates the log into
`feedback-sent.json`, so repeating `send` is a safe `no_edits` and a later
search reusing the same slug starts a fresh log.

## Execution Rules

- Never spend before the checklist-item-3 confirmation. In interactive deep mode, also wait for
  the required pond query/payload review; in auto deep mode, the approved plan authorizes the loop.
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
