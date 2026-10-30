---
name: search
description: "Find and rank people in your network or search from a job description. Use for discovery, recruiting and shortlists. A named person's dossier, or a list of people by relationship, label or tag (\"show me my family\"), is Deep Context's. Supports local and Powerset networks; company, SQL, and contact browsing use their own surfaces. Formerly $search-network."
---

# Search

Find and rank people through local or Powerset network retrieval.

Use this for discovery and recruiting requests:

- `$search software engineers in sf`
- `$search local: product managers in nyc`
- `$search https://jobs.lever.co/company/abc123`   ← deep JD → runs deep mode
- `$search senior engineers at series a fintech companies`
- `$search stanford engineers with 3-5 yoe in new york`
- `$search people who work at OpenAI`

> `$search` supersedes `$search-network` (the old name still works as an alias). The retrieval
> primitive is still `search_network_pipeline.py` — only the skill/route was renamed.

For a named person's dossier, labels or relationships, or a list of people the user
already knows by relationship, label, tag or worth ("show me my family", "who is tagged
investor", "my close friends"), follow `packs/ingestion/skills/deep-context/SKILL.md`
(its lookup and people reads) and stop here. Those are reads of the People list, not searches.
Explicit shared-network requests and recruiting stay on this skill. Requests
for people like someone continue through search below.

## How to run this skill

Deep JD searches send every hydrated candidate to the Jev capability screen; there
is no separate cheap filter (`--capability-judge terra` restores the Luna filter and
Luna rating screen). Candidates that pass receive parallel Terra qualification and Luna
opportunity judgments. Terra independently rechecks Luna opportunity cap 2;
overall is the lower of qualifications and the authoritative opportunity cap.
The viewer shows overall score and one explanation, sorted
by overall then capability. Ratings 1–2 show "Did not pass screen". Saved human
ratings remain separate. Ordinary non-JD searches keep their existing reranker.
Deep-mode results also get a private hosted snapshot when signed in to Powerset;
`deep-mode.md` owns upload and refresh. Explicit offline/local-only requests stay local.

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
   - `people` — any search for people, "my contacts" included. The default when unsure.
   - `company` — the subject is companies (lookup / IDs / investors / funding / sector) and
     no people are asked for. "Engineers at companies backed by Sequoia" is `people`.
   - `sql` — the predicate needs cross-row or cross-person logic: per-person aggregates
     ("2+ startup stints"), role ordering ("engineers who became PMs"), or a join against
     another person ("overlapped with Jane at Stripe"). A person's name alone is NOT sql:
     "look up Jane Doe" and "who is Jane Doe" are `people` lookups. Common words like
     "career" or "worked with <a technology>" do not make a query sql.
2. **backend** — which index runs the search. The user's explicit words always win:
   - `powerset` — the user says "powerset", names a set, or says "team/shared network"
     (even if they also say "my network": "search my Powerset network" is `powerset`).
   - `local` — the user says "local", "offline", or "my imported network/contacts",
     even if remote credentials exist.
   - Unstated → the chat's scope, which the pipeline also defaults to: `local` when
     `POWERPACKS_LOCAL_SEARCH_DB` is set in the environment, otherwise `powerset`. Say which in
     one line.
   - Forced values: `sql` is always `local`; `company` is always `powerset`.
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
stable slug from the query (e.g. `swe-sf-stanford`) and write `decision.json` with this command
and nothing else (no Python: the system interpreter has none of the project's packages):

```bash
mkdir -p .powerpacks/search/<slug> && cat > .powerpacks/search/<slug>/decision.json <<'EOF'
{"surface": "people", "backend": "<local or powerset>", "depth": "fast", "mode": "interactive",
 "reason": "<one sentence on why>"}
EOF
```

The environment default for `backend` is one check that prints nothing secret (the desktop app
pins a chat's scope into the environment; a command-line install may hold it in `.env`):
`{ test -n "$POWERPACKS_LOCAL_SEARCH_DB" || grep -q '^POWERPACKS_LOCAL_SEARCH_DB=.' .env 2>/dev/null; } && echo local || echo powerset`.

- fast → `.powerpacks/search/<slug>/decision.json`, and pass the same dir as `--output-dir`
  to `prepare` so the decision, payload, and outputs live together.
- deep → `.powerpacks/deep-search/<jd-slug>/decision.json` (the engine's existing run dir).

Then dispatch — this table is the whole routing contract:

| decision | action |
|---|---|
| surface `company` | load `packs/search/skills/search-company/SKILL.md` (decision.json still written first) |
| surface `sql` | load `packs/search/skills/search-sql/SKILL.md` (decision.json still written first) |
| `people` + `fast` + `local` | **Local Happy Path** below (`search_network_pipeline.py prepare --backend local --db <db>`) |
| `people` + `fast` + `powerset` | **TurboPuffer Happy Path** below (`search_network_pipeline.py prepare --backend powerset`) |
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
(`Search prepared. Execute or modify?`). Deep mode confirms its initial query and filters once — the only approval in
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
   flags 0 matches or a suspiciously narrow pool, recommend `modify`. Then say exactly this
   and nothing else (the chat shows the prepared search as a card):

   `Search prepared. Execute or modify?`

5. Apply the reply using **Execute or modify the preview** below.

6. Follow **Execution progress** below while the command runs.

### SQL assistance (local only)

For cross-row evidence, explicit SQL assistance, or zero matches in the preview
or results, load [search-sql](../search-sql/SKILL.md#integration-with-a-parent-search)
and follow its parent-search integration instructions. Ordinary row-level filters
do not need SQL assistance.

### Local Constraints

- LLM filter/rerank run by default and need `OPENAI_API_KEY`; if it is
  missing, rerun with `--search-only` instead of failing the search
- No set/operator resolution
- No TurboPuffer or Postgres calls
- Investors and set interactions have no local data: `prepare` drops those keys with a note.
  Interactions with you (`operator_interaction_min/max`) are a local filter.

---

## TurboPuffer Happy Path

Do not inspect repo docs, source, memory, prior transcripts, or prior result
files on the happy path. Start a fresh run for every search request.

1. Run:

   ```bash
   uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
     --backend powerset \
     --query "<user query>" \
     --output-dir ".powerpacks/search/<slug>"
   ```

   Use the same `<slug>` run dir where `decision.json` was recorded. (Deep-engine delegated
   profile searches pass their own output dir; follow the engine's instructions there.)

2. If `prepare` returns a preview, say exactly this and nothing else (the chat shows the
   prepared search as a card):

   `Search prepared. Execute or modify?`

3. Apply the reply using **Execute or modify the preview** below.
   `execute_command` already includes `--execute-approved`; do not ask for another approval.
   - If a **limit** was provided (e.g. by the deep-search engine for a
     capped profile search), append `--limit <N>` to the execute_command (or
     pass `--limit` to `prepare`, which threads it through). This caps
     retrieval and the whole downstream pipeline. For standalone user
     searches, do not add a limit unless the user asks for one.
4. Follow **Execution progress** below until the command finishes or emits a
   concrete `blocked_approval` / `blocked_user_action`.

## Execute or modify the preview

For both local and Powerset previews:

- `execute`: run the returned `execute_command` exactly in the background and read
  its output as it arrives.
- A reply starting `Modify, then execute:` is followed by one JSON object with any of
  `query` (a string), `filters` (an object: `role_search_filters` keys with their JSON values as
  the payload holds them, `null` to remove a key) and `traits` (the whole list of
  `{value, temporal, meaning}`). The card sends only what the user changed against the preview
  it showed: a key in `filters` is an edit, `traits` is present only when the traits changed,
  and `query` only when the query changed. Save the object exactly as printed, never re-parsed,
  to `<run>/card-edits.json`, where `<run>` is the preview's `--output-dir`:

  ```bash
  cat > <run>/card-edits.json <<'EOF'
  <the JSON object as printed>
  EOF
  ```

  The pipeline applies the edits and logs them in `<run>/user-edits.jsonl`: never edit the
  payload file yourself and never log these edits yourself. Then:
  - If `query` is present, or a trait's `value` changed, was added, or was removed against the
    `traits` of the preview you showed (an empty `traits` list clears them): run a new `prepare`
    with the same `--output-dir`, `--edits-json <run>/card-edits.json`, and `--query` set to the
    sent `query` (restated around the sent traits when both came) or, when only the traits
    changed, to the preview's query restated around the new traits. Keywords and filters never
    go into the query: the filter edits ride along through `--edits-json`, and the traits come
    from the restated query. Then show the new preview and ask the usual question, even though
    the reply said `then execute`: a new prepare is a new search. Only `execute` on the new card
    runs it; never run the old `execute_command` for a changed query or trait value.
  - Otherwise run the returned `execute_command` with ` --edits-json <run>/card-edits.json`
    appended, in the background, and read its output as it arrives. Filter and temporal-only
    edits need no second `prepare` or approval.
- A reply starting `Modify:` (no "then execute") comes from a blocked card: save the object the
  same way and run a new `prepare` with the same `--output-dir`, `--edits-json`, and the query by
  the rule above (the preview's own query when none was sent); then ask the usual question
  again. Never run the search from this reply.
- Any other reply is a modify in the user's words: write the same JSON object yourself and
  follow the rules above.

## Execution progress

Keep execution quiet except for these milestones from `run`:

- When it prints the screened count, say at once: `<N> screened, not ranked yet`; do not
  present a ranked shortlist before ranking finishes.
- When it prints the ranked count, say ranking has finished.

## Final Summary

- Say `<N> found`.
- For local results, say `found (local)`.
- In the Powerpacks desktop app (the developer instructions say you are its assistant), stop
  there: the results pane beside the chat shows the ranked list.
- Outside the app: say `Run artifacts: <artifact-dir>` and show the top 10 ranked rows from the
  run's `csv` artifact as a Markdown table (rank, name, title/company, location, LinkedIn URL).

## User edit & feedback capture

Query, filter and trait edits made through the card are logged by the pipeline (`--edits-json`
and a re-prepare over the same `--output-dir`). Log pond edits and result notes yourself,
immediately:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py log \
  --run-dir <run> --kind <pond_edit|result_feedback> \
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

- Do not run the search before the user answers "Search prepared. Execute or modify?"; never
  quote this rule or the checklist to the user.
- Do not run doctor or setup checks before a normal search unless the primitive
  fails with an unclear auth/env/setup error.
- Do not use sub-agents for ordinary single-query searches except the local
  SQL assistance described above.
- Do not write new retrieval scripts during a search run.
- For refinements after execution, create a new search with the updated query
  or constraints. Before execution, edits go through `--edits-json` as described above.
- Every Python command runs as `uv run --env-file .env --project . python …`; the system
  `python3` has none of the project's packages.
- Do not mention skip-rerank, alternate execution modes, internal ledgers, or
  internal artifact paths in the user-facing preview.

The packaged primitives own extraction, resolution, filtering, reranking, and
persistence. Treat their output as authoritative and inspect internals only
after a failed or inconsistent run, or when the user asks to debug.
