# Powerpacks agent guidance

This file is the canonical bootup instruction sheet for any coding agent
(Codex, Claude Code, NanoClaw, pi, etc.) working in the `powerpacks` repo.

`CLAUDE.md`, `.cursorrules`, etc. are symlinks to this file. If they drift,
re-run `bin/sync-agent-files.sh` from the repo root.

---

## Sub-agent delegation

The user explicitly authorizes Codex to use sub-agents for this repo. If skills
request sub-agents, use them. Leverage sub-agents to keep the main conversation
clean and concise.

---

## Local context

`PROFILE.md` is the tracked source template for clone/user-specific agent
context. Install/bootstrap renders it into harness-specific local files, such as
`.codex/AGENTS.md` for Codex, and appends non-secret context such as the
authenticated Powerset email and default set ID.

If `.codex/AGENTS.md` exists, use it for simple self-introspection questions.
Do not run network refreshes, doctor checks, MCP calls, or skill workflows just
to answer those questions.

Run `bin/agent-bootstrap` only when local context is missing/stale, after
install, or when the user asks to refresh it. It writes `.codex/AGENTS.md` and
`.powerpacks/memory/*.json`; these are local generated state and should not be
committed.

Do not paste secret env values into chat.

## Python setup

Search primitives depend on the repo Python project (`pyproject.toml`) for
TurboPuffer, OpenAI, Snowball stemmer, and Postgres packages. Install/setup
scripts run:

```bash
bin/setup-python
```

On first real work in this repo, if `.venv/` is missing or Python dependencies
are not ready, run `bin/setup-python` before running primitives. On macOS it may
install missing `uv` through Homebrew automatically. If Python, Homebrew,
Command Line Tools, or another OS-level prerequisite is missing, run the exact
command printed by setup/doctor and continue unless the command needs a password
or visible human action.

If a primitive reports missing Python packages, treat that as a setup problem:
run `bin/setup-python` or rerun the harness install script. Do not add runtime
package installation to primitives.

## Health check

```bash
bin/doctor run
```

Do **not** run the doctor as a routine preflight. A normal Powerpacks install
should already render the right `.env` values and local agent context, so start
with the narrow command or local artifact that matches the request.

Run the doctor only when there is a concrete setup signal and the next fix is
not already obvious, for example:

- `.env` is missing, unreadable, or lacks a key needed by the primitive you are
  about to run.
- A primitive fails with an auth, missing-env, local-permission, network,
  Docker, or OS-access error and the failing layer is unclear.
- The user explicitly asks for a health check, setup audit, or installation
  diagnosis.
- You are recovering from a failed install/bootstrap and need the doctor's JSON
  report to decide the next fix.

Do not run it for simple self-introspection questions answered from
`.codex/AGENTS.md`, for searches when `.env` is present and the primitive can
report its own error, or for messages/contact workflows before you have tried
the scoped readiness command for the needed surface.

The doctor emits a JSON report with one entry per check (`status` is one of
`ok | warn | missing | fail`). If anything is `missing` or `fail`, surface it to
the user before continuing. For `warn`, mention briefly and proceed.

If the user explicitly asks for a fix and the doctor reported a fixable
issue (each check carries a `fix_command`), only then run:

```bash
bin/doctor fix
# or for browser-based logins:
bin/doctor fix --interactive
```

Never run `fix` unprompted. Browser logins, gcloud auth, Full Disk Access,
Docker/WAHA QR setup, and spend-bearing operations are user-consent operations.
Python dependency setup via `bin/setup-python` is part of normal onboarding.

For Powerset users, proactively distinguish expired/reauth-needed credentials
from IAM/secret provisioning failures. If any `gcloud` command fails with auth
expiration / reauthentication text (for example `problem refreshing your current
auth tokens`, `Reauthentication failed`, or `cannot prompt during
non-interactive execution`), the fix is:

```bash
gcloud auth login --no-launch-browser
```

Run it from this shell after user consent, relay the verification URL/code
prompt, and ask the user to paste the code. If `.env` already has the requested
profile keys, expired gcloud Secret Manager access is not a blocker and should
not be surfaced unless the user asks to refresh/provision secrets. If `.env` is
missing keys and the doctor reports `user_secrets` with `fix_kind: interactive`,
tell the user their selected `@powerset.co` account is fine but gcloud's cached
token expired. Do not route this to Slack and do not run ADC setup;
application-default credentials are not needed for normal Powerpacks workflows.

## Workspace state

If `.powerpacks/` exists in the working directory, list its contents (or
`tree -L 2 .powerpacks/` if available) so the user can see what artifacts
are already present from prior runs. Don't rebuild artifacts that already
exist unless explicitly asked.

## Pack-specific readiness

- **Messages pack** (iMessage / WhatsApp imports, contact review):
  - `chat.db` access: requires Full Disk Access on macOS. Check it with the
    iMessage primitive's scoped `check` command when doing iMessage work; if
    access is not granted, *stop* and ask the user to enable it in System
    Settings before retrying.
  - WAHA container: only needs to be up if the user is doing WhatsApp work.
    Run `uv run --project . python packs/messages/primitives/waha_runtime/waha_runtime.py status`
    on demand, not on every bootup.
  - Powerset login: required for `sync_powerset_candidates`, `upload`. Check
    via `uv run --project . python packs/powerset/primitives/auth/auth.py whoami`.
- **Search pack** (search-network, search-company): requires `.env` with
  TurboPuffer + Postgres credentials. If `.env` is present, run the search
  primitive directly and use its error to diagnose; use the doctor only if env
  or auth looks broken and the cause is unclear.

Don't run pack-specific checks pre-emptively. Only when the user's request
implies that pack.

---

## Natural skill routing

When the user asks for an outcome that matches a Powerpacks skill, proactively
load and follow that skill's `SKILL.md` even if the user did not type a slash
command. Treat skills as the natural harness for this repo, not just explicit
commands.

Common routes:

- people/network/company-directory queries → `packs/search/skills/search-network/SKILL.md`
- company set resolution / company IDs → `packs/search/skills/search-company/SKILL.md`
- my contacts / set contacts → `packs/contacts/skills/search-contacts/SKILL.md`
- Sales Navigator leads → `packs/sales-nav/skills/sales-nav-search/SKILL.md`
- Powerset login / MCP install / credentials → `packs/powerset/skills/powerset/SKILL.md`
- iMessage / WhatsApp / contact imports → `packs/messages/skills/import-contacts/SKILL.md`

Do not ask the user to pick a skill when the route is obvious. Do ask a brief
clarifying question when the request could mean multiple surfaces, e.g. "people
at OpenAI" (company directory) vs "AI engineers at OpenAI" (semantic/role
search), or "contacts at OpenAI" (contacts field filter).

## Skill behavior overrides

These are nudges that override defaults baked into individual `SKILL.md`
files. They apply to every session in this repo.

### search-network

Default behavior in `packs/search/skills/search-network/SKILL.md` is a 16-step
strategy loop with mandatory approval gate. That is correct for messy/broad
queries. For narrow, unambiguous queries, **skip the loop**:

- If the query is only "people who work at <company>" / "employees of
  <company>" with no role/title/seniority/domain constraint, use the
  company-directory fast path: call MCP `list_company_people`, page results,
  and do not run semantic people search or local retrieval primitives.
- A query is otherwise narrow when it has a single named company, a single named
  person, ≤ 3 hard filters, or obvious structure.
- For narrow non-directory queries, run only: `task_state init` →
  `resolve_companies` (or `resolve_education` / `resolve_investors` if relevant)
  → `execute_role_search` → `hydrate_people` → `persist_search_results`. Five
  primitives, no approval gate, no slicing, no count, no LLM filter, no agentic
  rerank.
- Do not invoke `plan_adjacency_search`, `decide_search_strategy`,
  `count_candidates`, `assess_frontier`, `plan_candidate_review`, or
  `llm_filter_candidates` for narrow queries. They are all no-ops on
  unambiguous input and just add turns.
- Run the full strategy loop only when the query is genuinely ambiguous
  ("engineers in SF", "stanford grads at fintech") or the user explicitly
  asks for slicing/rerank.

### import-contacts downstream review TUI

- The TUI fix shipped in `contact-exporter` v0.1.25. If the user reports
  TUI weirdness, check `contact-exporter --version` first.
- Bucket counts in our research-review CSV map to TUI tabs as
  `confident → yes`, `medium → maybe`, `review → no`. The server's
  `yes_count / maybe_count / no_count` on `/v2/messages-research/artifacts`
  upload uses the same accounting.

### deep_research_contacts (Parallel.ai)

- Parallel contact research may only use these processors: `core`, `core2x`, and `pro`.
- `submit` and `poll` can be split if the user is okay with backgrounding.
  For a small batch (< 30 contacts) just `run`. For larger queues,
  recommend `submit` + come back later for `poll`.
- Idempotency: re-runs skip handles that already have
  `01_research_parallel.json`. Safe to re-run.
- If prior-cache sync / `gcloud storage rsync` fails because gcloud auth expired,
  stop before any paid Parallel submit/run. Reauthenticate with
  `gcloud auth login --no-launch-browser`, rerun the sync, then estimate. Never
  proceed to paid research just because the cache sync could not authenticate.

---

## General defaults

- **Be terse on operational status.** Print one-line summaries of what
  primitives wrote / what counts came back. Do not narrate the whole plan.
- **Don't ask permission for read-only operations** (TurboPuffer filter
  searches, local file reads, scoped `check`/`status` commands, `whoami`,
  `estimate` subcommands, and doctor `run` when it is actually needed by the
  health-check policy). Ask only for spend (LLM calls, Parallel.ai submits,
  uploads, Docker pulls, browser-based logins, OS installs).
- **Stdlib-only is a hard constraint** for new primitives in this repo. No
  `requests` / `pydantic` / `httpx` / SDK dependencies.
- **Test additions** go in `tests/` and run via `uv run --project . python -m unittest discover -s tests`.
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
│   │   ├── skills/             # user-facing skill (import-contacts)
│   │   ├── tasks/              # task JSON specs
│   │   ├── docs/
│   │   └── README.md
│   ├── search/                 # search-network, search-company
│   └── powerset/               # cross-pack tooling (doctor, auth, ...)
├── skills/                     # core skills (search-network, search-company)
├── tests/                      # unittest, run with uv run --project . python -m unittest discover
├── adapters/codex/install.sh   # installs skills into ~/.codex/skills
├── bin/                        # update-codex, update-claude-code, agent-bootstrap, sync-agent-files.sh, etc.
├── PROFILE.md                  # source template for generated local profiles
├── .codex/AGENTS.md            # ignored Codex profile rendered from PROFILE.md
├── .powerpacks/memory/         # ignored project-local agent memory
└── AGENTS.md                   # this file
```
