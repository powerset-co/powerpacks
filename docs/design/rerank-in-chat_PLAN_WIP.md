# Rerank in the main chat — design

Status: proposal, no code changed. Written against powerpacks `e4a61bc9` (main) and the
network-search-api checkout. Paths are relative to each repo.

## TL;DR recommendation

Make rerank a **follow-up turn in the same Codex thread, acting on the same search run**: the user
types "only people who shipped developer tools" and the search skill runs one new subcommand,
`search_network_pipeline.py rerank --run-dir .powerpacks/search/<slug> --criteria "…"`, which
re-scores the run's *existing* hydrated frontier with `llm_rerank_candidates` (already the plain
search's final step) and exports a **new scoring pass on the same run id**. Nothing new is wired
through Codex: the set/backend already reach the pipeline as `shell_environment_policy` env on
`thread/start`/`thread/resume` (`desktop/src-tauri/src/codex.rs:326-345`), and "the current search"
is already known to both sides — the agent from its own transcript (it printed `--output-dir`), the
page from `chatRun()` regex over the commands (`web/src/lib/agent/searchRun.ts:8-20`). The results
pane shows the active pass's criteria chips and a plain "pass N of M" switch; no sub-threads, no
rollback tree, no new tables. Previous passes stay as files (`<task>.pass<N>.jsonl`) so switching is a
fetch of one JSONL, not a recompute.

## How set + backend mode reach Codex today

This is the pattern to reuse. It is **env override per thread, through Codex's config**, not prompt
injection and not `.env` (commit `19fbb7b6` "a chat's search scope is pinned through Codex, not .env").

| Step | Where |
| --- | --- |
| Page keeps the picked scope (`{set_id}` or `{local_db}`) until the first send; a chat that has started keeps its scope | `web/src/lib/agent/store.ts:36-38, 246-251` (`setScope`), `store.ts:202` (`startThread(state.scope)`) |
| Tauri command `codex_start_thread(scope)` → `Codex::start_thread` | `desktop/src-tauri/src/lib.rs:128-137`, `codex.rs:348-361` |
| `thread_settings()` builds `thread/start` params: `cwd`, `approvalPolicy: on-request`, `sandbox: workspace-write`, `developerInstructions`, and `config.shell_environment_policy.set = {POWERPACKS_DEFAULT_SET_ID, POWERPACKS_LOCAL_SEARCH_DB}` (both keys always, unused one empty so a stale `.env` fills neither) | `codex.rs:326-345` |
| Thread → scope persisted so reopening resumes with the same env: `.powerpacks/desktop/chat-scopes.json`; `thread/resume` re-sends the same `config` | `desktop/src-tauri/src/scopes.rs:9-41`, `codex.rs:365-372` |
| Every command Codex runs for that thread inherits the env; the pipeline's `--backend`/`--db` default from it | `packs/search/primitives/search_network_pipeline/search_network_pipeline.py:1352-1358` (`add_backend`), `packs/search/primitives/shared/search_backend_mode.py:21-29`, `packs/search/primitives/shared/search_common.py:341` |
| The skill tells the agent the env is the default backend | `packs/search/skills/search/SKILL.md:82-84` |
| Per-turn knobs go on `turn/start` instead: `approvalPolicy`, `sandboxPolicy`, `model`, `effort` | `web/src/lib/api/codex.ts:98-102, 151-164` |

Two other precedents matter for this design:

- **Prompt-side context injection exists but is minimal**: a chat opened from a saved search prefixes
  its first message with `About the search saved in .powerpacks/deep-search/<run>: …`
  (`store.ts:195-199`). Everything else the agent knows about "the current search" is its own
  transcript, which Codex replays on `thread/resume` (`decode.ts:145-157`).
- **The page identifies the chat's search by reading the agent's commands**, not by any thread
  metadata: `chatRun()` takes the newest `.powerpacks/(deep-)search/<run>` mentioned in a `command`
  or `user` entry that the catalog lists (`searchRun.ts:8-20`), falling back to `agent.searchRun`
  (`AgentPage.tsx:225`). The run ↔ thread link is a localStorage key `search-thread:<runId>`
  (`store.ts:205`, `AgentPage.tsx:245-249`, `Chats.tsx:47-53`).

## What Codex app-server offers (as used here, v0.161.0)

Bundled binary: `@openai/codex` **0.161.0** (`desktop/scripts/bundle-runtime.sh:14`), spoken to as
JSON-RPC over stdio with the v2 app-server methods. Evidenced in the repo:

- `initialize`/`initialized`, `thread/start`, `thread/resume`, `thread/list`, `thread/archive`,
  `thread/read`, `turn/start`, `turn/interrupt`, `model/list`, `account/*` (`codex.rs:43-51, 258-267,
  353-381`; `codex/questions.rs:310-345`; `codex.ts:28-168`).
- `thread/start` / `thread/resume` accept `cwd`, `approvalPolicy`, `sandbox`, **`config`** (a config
  overlay; the app uses `sandbox_workspace_write` and `shell_environment_policy`) and
  `developerInstructions` (`codex.rs:326-345`). This is the only env-override hook the app relies on.
- `turn/start` accepts `input[]`, `approvalPolicy`, `sandboxPolicy`, `model`, `effort` (`codex.ts:157-163`).
- Server → client requests the app answers: `item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`, `item/permissions/requestApproval`, `item/tool/requestUserInput`
  (`decode.ts:213-241`, `codex.ts:197-210`).
- Notifications/items the page decodes: `turn/started|completed`, `item/started|completed`, deltas for
  agentMessage/reasoning/commandExecution output, `error`; item types `agentMessage`, `reasoning`,
  `commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`, `userMessage` (`decode.ts:69-115, 166-198`).
- A second, programmatic thread pattern exists (shared questions): `thread/start` → `turn/start` →
  wait for `turn/completed` and parse the final `agentMessage` (`codex/questions.rs`). Useful as a
  template, but **not** needed for rerank-in-chat.
- Codex's rollout files under `CODEX_HOME/sessions` are readable by the app (`codex.rs:399-440`).

**To verify (not evidenced in the repo; `main`'s app-server README mentions some of these but 0.161.0
may differ):** per-turn `config` overrides on `turn/start`; client-registered "dynamic tools"
(`item/tool/call`); `thread/settings/update` / `thread/metadata/update`; thread forking
(`forkedFromId`). The design below needs none of them — it only uses `config.shell_environment_policy`
on `thread/start|resume` (already used) and ordinary `commandExecution` items.

## Current rerank code (what exists to reuse)

A plain (non-JD) search is `prepare` → `run`: retrieval → `hydrate_people` → `llm_filter_candidates`
(screen) → `llm_rerank_candidates` (score) → `persist_search_results` export → `record_quick_run`
(`search_network_pipeline.py:983-1016` powerset, `:1096-1125` local; commit `9a91bb36`).

- **Rerank primitive** `packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py`:
  `--state` mode reads the frontier (`state_frontier_ids`, `:835`) and the full hydrated profiles
  (`load_items_from_state`, `:963-977`), takes criteria as `--evaluation-query` and
  `--evaluation-traits-json` (`:1179-1182`, overriding `state_traits()` `:901-921`), fans out one LLM
  call per person (`rerank_all`, prompt at `build_user_prompt` `:474`, same prompt family as the cloud
  app — `PROMPT.md`), writes `artifacts/<task>/llm_rerank_candidates/query_results.csv` and
  **appends** an `llm_rerank_candidates` step to the state with `ranked_candidate_ids`,
  `evaluation_query`, `evaluation_traits` (`:1355-1395`, `record_state_step` `:1081-1100`). Steps are
  append-only and readers take the **last** step of an id (`results_io.py:87-91`,
  `search_network_pipeline.py:888`) — so a second rerank pass is already "latest wins" with zero changes.
- **Export** `packs/search/primitives/persist_search_results/results_io.py`: `result_rows()` (`:219`)
  joins the latest rerank CSV (`rerank_rows` `:105`) onto `frontier_ids()` (`:121`, prefers
  `ranked_candidate_ids`); `cmd_export` (`:316-351`) writes `<stem>.csv/.jsonl/.manifest.json` and
  already supports `--out-dir` and `--name` (`:377-378`) — i.e. per-pass file names cost nothing.
- **Run record for the viewer**: `record_quick_run` (`search_network_pipeline.py:1157-1175`) →
  `record_quick_search` (`deep_search/results_web/model.py:812-839`) writes
  `.powerpacks/deep-search/<slug>/results.json` with one pond whose `summary.pond_chain[0]` carries
  `query`, `filters`, `traits`, and `iterations[0].arm.artifacts.jsonl` → the rows; plus a card in
  `catalog.json` with `status` (`running` after screening via `_export_screened` `:1196-1206`,
  `completed`/`failed` from `cmd_run` `:1208-1232`).
- **Page**: `/searches/api/catalog` and `/searches/api/search.json?run_id=` (`results_web/api.py:61`,
  `packs/shared/web/server.py:131`) → `useCatalog` polls every 3 s while the agent runs
  (`web/src/pages/searches/hooks/useCatalog.ts`), `useSearchRun` re-reads the run when its status
  leaves `running` (`AgentPage.tsx:238-243`). `RunView` → `SearchFilters` shows the pond's query,
  **traits as chips** (`TraitChips`) and filters (`web/src/pages/searches/run/SearchFilters.tsx`);
  `RunHeader` shows a status badge until `completed` (`RunHeader.tsx:33-36`, `lib/searches/copy.ts:7-18`).
- **Skill**: `packs/search/skills/search/SKILL.md` — the agent prepares, shows the card, runs
  `execute_command` in the background, reports `<N> screened, not ranked yet` then `<N> found`
  (`:355-370`), and today is told: *"For refinements after execution, create a new search"* (`:404`).
  That line is what changes.

## The old implementation Arthur hated (cloud agent), briefly

`network-search-api/api_v2/agents`: a LangGraph ReAct agent (`react_graph.py`) where every post-search
operation (`query_results` SQL filter/sort, `score_results` LLM rescore, `review_results`) is a new
**`AgenticInteraction` row chained by `parent_interaction_id` with exactly one `is_current=True`**
per conversation (`db/schemas.py:1196-1267`; `tools/rerank.py:1-100` docstring: "Interaction
Chaining"). Each step persists its own `agentic_results` rows; chaining re-loads the parent's ids with
score/trait-score filters that differ by interaction type (passthrough vs filtered, `rerank.py:60-80`),
the API exposes `parent_interaction_id` "for branching" (`chat_endpoint.py:283-289`), and `rollback`
flips `is_current` back to any earlier node (`tools/conversation.py:132-202`, `react_tools.py:510-527`,
prompt at `react_graph.py:538`). So every rerank was effectively a sub-thread with its own result set,
a tree the UI had to render and the agent had to reason about (which parent? which score semantics?),
with per-type rules to stop the chain from silently dropping people. Heavy because: a DB row per step,
three interaction types with different score meanings, a current-pointer that must stay unique,
rollback as a first-class tool, and a classifier call to route every follow-up.

powerpacks git history has no local version of this: `git log -S rollback` only hits the People/Searches
React app merge (`89eb3885`), and `-S "sub-thread"`/`subthread` hit nothing. The local rerank lineage is
the pipeline step only (`a834f814`, `8524a196`, `9a91bb36`).

(“astra”: not a tool in either repo. It appears only as the model id `gpt-6-astra` in
`web/src/lib/agent/models.test.ts:9` and as "Astra's review" in commit bodies such as `19fbb7b6`, i.e.
a GPT model used as a reviewer in Arthur's workflow; nothing here to wire up.)

## Proposed data flow

### 1. The user asks in the chat

A normal follow-up message in the same thread: "rerank for people who shipped developer tools",
"only founders", "prefer people who were early at a Series A". No new UI affordance is required for
v1; an optional "Refine…" field in the results pane can just `send()` the same text later.

### 2. The agent runs one primitive (skill change)

`SKILL.md` gets a short **"Refine a finished search"** section replacing line 404:

- If the chat has a finished quick search (the agent ran `--output-dir .powerpacks/search/<slug>` in
  this chat — it is in its transcript, replayed on resume) and the follow-up narrows or re-orders
  *those* people rather than asking for new retrieval, run in the background:

  ```bash
  uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py rerank \
    --run-dir .powerpacks/search/<slug> \
    --criteria "<the user's words, one line>"
  ```

- Say `<N> re-ranked for "<criteria>"` when it prints `search: N ranked (pass K)`; stop. The pane shows it.
- New retrieval terms (new location, new company, "also include…") stay a new `prepare` — the primitive
  cannot add people, only re-score the ones the run already hydrated. The agent decides this in words; no
  classifier.

The run dir is passed explicitly rather than through env: the env is per-thread (`thread/start`) and a
thread can hold several searches; the agent already names the dir in every command (that is how the
page finds it too). If we later want it machine-pinned, the page knows `runId` and can prefix the message
the way `store.ts:195-199` does — a one-line change, still no Codex feature needed.

### 3. `rerank` subcommand (pipeline change, the only real new code)

`search_network_pipeline.py rerank --run-dir <dir> --criteria <text> [--traits-json <file>] [--model …]`:

1. Load `<dir>/pipeline.ledger.json` → state; refuse unless `hydrate_people` is done (and the run is not
   `running`).
2. Build the pass's criteria:
   - `evaluation_query` = the run's query + `\nRefinement: <criteria>`.
   - `evaluation_traits` = the run's current traits + one trait `{value: <criteria>, temporal: "all",
     meaning: "general"}` (or the agent's `--traits-json` when it chose to split the sentence). Local and
     cloud both score "query + traits" through the same reranker prompt, so this keeps the two in step.
3. Run `llm_rerank_candidates.py --state … --evaluation-query … --evaluation-traits-json … --write-state`
   over the **screened frontier** (`llm_filter_candidates.passed_candidate_ids`, not the previous pass's
   order — each pass is scored from the same pool, so passes are comparable and there is no chain to
   unwind). `frontier_ids()` prefers `ranked_candidate_ids` today; the subcommand passes the pool
   explicitly (small change in `load_items_from_state`, or a `--frontier passed` flag).
4. Export with `results_io.py export --state … --name <task>.pass<K>` → `<task>.pass<K>.jsonl`;
   `pass1` is the original export (rename-on-first-rerank or just record its existing path).
5. Record the pass in the run (`record_quick_run` → `record_quick_search`): mark the card `running`
   when the pass starts (so the pane shows the badge and keeps the previous rows, exactly as a forced
   rerun does today) and `completed` when it lands.
6. Print `search: N ranked (pass K)` on stderr and the usual JSON summary on stdout.

Ledger: `mark(lp, l, "llm_rerank_candidates", …)` is overwritten per run today (`:184-187`); add a
`passes` list to the ledger with `{pass, criteria, traits, started_at, finished_at, jsonl}`. Cost: one
LLM call per screened person, same as the original rerank.

### 4. Where the reranked output lives

Same run id, same `results.json`, same catalog card. Added to `results.json`:

```json
"passes": [
  {"pass": 1, "criteria": null, "traits": [...], "jsonl": ".../<task>.pass1.jsonl", "created_at": "…"},
  {"pass": 2, "criteria": "shipped developer tools", "traits": [...], "jsonl": ".../<task>.pass2.jsonl", "created_at": "…"}
],
"current_pass": 2
```

`iterations[0].arm.artifacts.jsonl` and `summary.pond_chain[0].traits` always point at the current pass,
so every existing reader (`load_search`, `_pond_candidates`, the catalog, tags, feedback, ask) keeps
working unchanged. A pass is a file, not a thread, not a DB row; there is nothing to roll back — "show
pass 1" is reading another file.

### 5. Results pane

- `SearchFilters` already renders `pond.traits` as chips. Show the pass's traits, with the refinement
  trait(s) visually distinct (e.g. a `Refined` chip style; `TraitChips` gets an `added` flag).
- Header gets a small **pass control** when `passes.length > 1`: `Pass 2 of 2 · "shipped developer
  tools"` with ‹ › to view an earlier pass. Viewing is read-only: `GET /searches/api/search.json?run_id=X&pass=1`
  returns the same payload with that pass's rows and traits (api.py builds it from `passes[k].jsonl`).
  Switching is one small fetch; the current pass stays cached. No "make this current" button in v1 —
  the next rerank always scores the screened pool, so the user is never stuck on a bad pass.
- Status: while the pass runs the card is `running`, `RunHeader` shows "Running" and the previous rows
  stay (this is the existing forced-rerun behaviour from `9a91bb36`); `useCatalog` polls while the agent
  runs; when status leaves `running`, `useSearchRun` re-reads once (`AgentPage.tsx:238-243`). Nothing new.
- Transcript: the rerank is an ordinary folded command (`Transcript.tsx` work panel). Optionally a
  one-line "Reranked for …" card by recognising `primitive == "search_network_pipeline"` with
  `"pass"` in the printed JSON (`preparedSearch.ts:86-101` already parses the last JSON object).

Instant feel: the only synchronous page work is one `search.json` read when the pass lands; the
pane never waits on Codex. Single writer: the pipeline process writes the run; the page only reads.
Local vs cloud: both backends share `run_pipeline*`'s rerank step and this subcommand sits above the
backend split, so local and Powerset runs rerank identically.

## Reused vs removed

Reused as-is:
- `codex.rs` `thread_settings` env override, `scopes.rs`, `codex.ts` — no Codex-side change.
- `llm_rerank_candidates.py` (`--evaluation-query`, `--evaluation-traits-json`, `--write-state`,
  append-only steps), `results_io.py export --name`, `record_quick_search`, `catalog.json` status flow.
- `useCatalog` polling, `useSearchRun` re-read on status change, `SearchFilters`/`TraitChips`,
  `RunHeader` status badge, `chatRun()` run detection, `search-thread:<run>` link.

New/changed:
- `search_network_pipeline.py`: `rerank` subcommand (+ `passes` in ledger; pool selection for the pass).
- `results_web/model.py`: `passes`/`current_pass` in `results.json`; `record_quick_search(..., passes=)`.
- `results_web/api.py`: `?pass=` on `search.json`.
- `web`: `Pond`/`SearchResult` types + pass control in `RunHeader`/`SearchFilters`; chip style for
  refinement traits.
- `SKILL.md`: "Refine a finished search" section; drop the "create a new search" rule at `:404`.

Explicitly **not** ported from the cloud agent: `AgenticInteraction`/`AgenticResult` chaining,
`is_current` pointer, `parent_interaction_id` branching, `rollback` tool, the `operations_classifier`
sort/filter/rescore router, per-type score semantics. Deterministic sort/filter ("sort by age") is out of
scope here — the pane's table already sorts, and SQL predicates belong to `search-sql`.

## Open questions for Arthur

1. Scope of a pass: always re-score the **screened pool** (comparable passes, no chain), or re-score the
   **previous pass's top-N** (cheaper, cloud-like)? I propose the pool.
2. Should a rerank pass **drop** people below a score (cloud drops < 0.5) or only re-order with scores
   visible? I propose re-order only; the pane can hide low scores.
3. Should a refinement **replace** the original traits or **add** to them? Proposed: add (cumulative
   across passes, each pass stores its full trait list).
4. Is a text field in the results pane ("Refine…" → sends to chat) wanted in v1, or chat-only?
5. Keep every pass's JSONL forever, or cap at the last ~5 per run?
6. Any need for the page to pin the run id into the message (`store.ts:195` style), or is the agent's
   transcript reliable enough? (Recommend: transcript only until it proves flaky.)

## Implementation plan (small PRs)

1. **Pipeline**: `rerank` subcommand + `passes` in the ledger; pass-named exports; `search: N ranked
   (pass K)`; tests in `tests/test_local_search_pipeline.py` style with the mock OpenAI server.
2. **Run record**: `passes`/`current_pass` in `results.json` via `record_quick_search`; `?pass=` on
   `search.json`; `running`→`completed` around a pass. Tests on `model.py`/`api.py`.
3. **Skill**: "Refine a finished search" section; remove the "new search" rule; wording for the
   two chat lines. Verify in the app that the agent picks `rerank` for "only people who…" and `prepare`
   for "also in NYC".
4. **Web**: types + pass control + refined-trait chip style; `RunView`/`RunHeader` tests.
5. (Optional) Transcript card line for a rerank, and a "Refine…" field in the pane that calls `send()`.
