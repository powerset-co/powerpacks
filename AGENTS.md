# Powerpacks agent guidance

This file is the canonical bootup instruction sheet for any coding agent
(Codex, Claude Code, NanoClaw, pi, etc.) working in the `powerpacks` repo.

`CLAUDE.md`, `.cursorrules`, etc. are symlinks to this file. If they drift,
re-run `bin/sync-agent-files.sh` from the repo root.

---

## Bootup sequence

When you start a session in this repo, **run these steps before doing real
work**:

### 0. Prefill project-local memory

```bash
bin/agent-bootstrap
```

This is read-only except for writing `.powerpacks/memory/*.json` (ignored by
git). It scans all project `.env*` files and records key presence only — never
secret values — then checks existing project memory for set IDs and, if the
Powerset login is valid, refreshes visible sets from `/v2/sets`.

Use these memory files during the session instead of rediscovering the same
facts repeatedly:

- `.powerpacks/memory/env_summary.json` — env files, keys present/nonempty,
  default set IDs from `.env`
- `.powerpacks/memory/set_ids.json` — visible Powerset sets with IDs, names,
  roles, member counts, personal/non-personal flag, and selected default set
- `.powerpacks/memory/agent_bootstrap.json` — short boot summary

If `sets_refreshed` is false, read `sets_error`; usually the user just needs to
run `powerset-login` or refresh credentials. Do not paste secret env values into
chat.

### 1. Health check

```bash
python3 packs/powerset/primitives/doctor/doctor.py run
```

The doctor emits a JSON report with one entry per check (`status` is one of
`ok | warn | missing | fail`). Read it. If anything is `missing` or `fail`,
surface it to the user *before* attempting the task they asked for. For
`warn`, mention briefly and proceed.

If the user explicitly asks for a fix and the doctor reported a fixable
issue (each check carries a `fix_command`), only then run:

```bash
python3 packs/powerset/primitives/doctor/doctor.py fix
# or for browser-based logins:
python3 packs/powerset/primitives/doctor/doctor.py fix --interactive
```

Never run `fix` unprompted. Browser logins, gcloud auth, OS-level installs
are user-consent operations.

### 2. Workspace state

If `.powerpacks/` exists in the working directory, list its contents (or
`tree -L 2 .powerpacks/` if available) so the user can see what artifacts
are already present from prior runs. Don't rebuild artifacts that already
exist unless explicitly asked.

### 3. Pack-specific readiness (only if relevant to the request)

- **Messages pack** (iMessage / WhatsApp imports, contact review):
  - `chat.db` access: requires Full Disk Access on macOS (the doctor checks
    this; if not granted, *stop* and ask the user to enable it in System
    Settings before retrying)
  - WAHA container: only needs to be up if the user is doing WhatsApp work.
    Run `python3 packs/messages/primitives/waha_runtime/waha_runtime.py status`
    on demand, not on every bootup.
  - Powerset login: required for `sync_powerset_candidates`, `upload`. Check
    via `python3 packs/powerset/primitives/auth/auth.py whoami`.
- **Search pack** (search-network, search-company): requires `.env` with
  TurboPuffer + Postgres credentials. The doctor covers this.

Don't run pack-specific checks pre-emptively. Only when the user's request
implies that pack.

---

## Skill behavior overrides

These are nudges that override defaults baked into individual `SKILL.md`
files. They apply to every session in this repo.

### search-network

Default behavior in `packs/search/skills/search-network/SKILL.md` is a 16-step
strategy loop with mandatory approval gate. That is correct for messy/broad
queries. For narrow, unambiguous queries, **skip the loop**:

- A query is narrow when it has a single named company, a single named
  person, ≤ 3 hard filters, or otherwise has obvious structure.
- For narrow queries, run only: `task_state init` → `resolve_companies` (or
  `resolve_education` / `resolve_investors` if relevant) → `execute_role_search`
  → `hydrate_people` → `persist_search_results`. Five primitives, no
  approval gate, no slicing, no count, no LLM filter, no agentic rerank.
- Do not invoke `plan_adjacency_search`, `decide_search_strategy`,
  `count_candidates`, `assess_frontier`, `plan_candidate_review`, or
  `llm_filter_candidates` for narrow queries. They are all no-ops on
  unambiguous input and just add turns.
- Run the full strategy loop only when the query is genuinely ambiguous
  ("engineers in SF", "stanford grads at fintech") or the user explicitly
  asks for slicing/rerank.

### import-contacts-review and downstream review TUI

- The TUI fix shipped in `contact-exporter` v0.1.25. If the user reports
  TUI weirdness, check `contact-exporter --version` first.
- Bucket counts in our research-review CSV map to TUI tabs as
  `confident → yes`, `medium → maybe`, `review → no`. The server's
  `yes_count / maybe_count / no_count` on `/v2/messages-research/artifacts`
  upload uses the same accounting.

### deep_research_contacts (Parallel.ai)

- `submit` and `poll` can be split if the user is okay with backgrounding.
  For a small batch (< 30 contacts) just `run`. For larger queues,
  recommend `submit` + come back later for `poll`.
- Idempotency: re-runs skip handles that already have
  `01_research_parallel.json`. Safe to re-run.

---

## General defaults

- **Be terse on operational status.** Print one-line summaries of what
  primitives wrote / what counts came back. Do not narrate the whole plan.
- **Don't ask permission for read-only operations** (TurboPuffer filter
  searches, local file reads, doctor `run`, `whoami`, container `status`,
  `estimate` subcommands). Ask only for spend (LLM calls, Parallel.ai
  submits, uploads, Docker pulls, browser-based logins, OS installs).
- **Stdlib-only is a hard constraint** for new primitives in this repo. No
  `requests` / `pydantic` / `httpx` / SDK dependencies.
- **Test additions** go in `tests/` and run via `python3 -m unittest discover -s tests`.
  Run the full suite after non-trivial edits.
- **Privacy contract**: no message bodies are ever read or sent. Only
  contact metadata (phone, name, source, group flags, message counts,
  last_message). Carry this through any new primitive.
- **Artifacts under `.powerpacks/`** are derivable. The agent can rebuild
  any of them from the source data; never paste full datasets into chat.

---

## File layout cheat sheet

```
powerpacks/
├── packs/
│   ├── messages/               # iMessage + WhatsApp + Powerset enrichment
│   │   ├── primitives/         # one subdir per primitive
│   │   ├── skills/             # user-facing skills (import-imessage, ...)
│   │   ├── tasks/              # task JSON specs
│   │   ├── docs/
│   │   └── README.md
│   ├── search/                 # search-network, search-company
│   └── powerset/               # cross-pack tooling (doctor, auth, ...)
├── skills/                     # core skills (search-network, search-company)
├── tests/                      # unittest, run with python3 -m unittest discover
├── adapters/codex/install.sh   # installs skills into ~/.codex/skills
├── bin/                        # smoke tests, agent-bootstrap, sync-agent-files.sh, etc.
├── .powerpacks/memory/         # ignored project-local agent memory
└── AGENTS.md                   # this file
```
