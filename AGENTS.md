# Powerpacks agent guidance

## Vorflux GitHub integration guardrail

For pull-request actions, prefer Vorflux's GitHub integration over raw `gh` commands:

- Use Vorflux PR tools for PR creation, editing, commenting, reviewing, and merging whenever available.
- Prefer the Vorflux GitHub App / platform integration identity when available. Do not default to a specific person's connected GitHub account unless the user explicitly asks for personal attribution or the platform reports that a connected-user token is the only available integration for the action.
- For PR creation, confirm the Vorflux tool reports that an integrated token was used, such as `used_user_token: false` for the app/bot path, `used_user_token: true` for an explicitly requested connected-user path, or an equivalent indicator.
- Do not use raw GitHub CLI/API calls for mutating PR actions unless unavoidable. If unavoidable, first run `gh api user --jq .login`, verify the active identity is appropriate for the requested action, and include the verified login in your status update.

This keeps PR authorship, merge attribution, and repository permissions aligned with the available Vorflux GitHub integration rather than an incidental shell token.


This file is the canonical bootup instruction sheet for any coding agent
(Codex, Claude Code, NanoClaw, pi, etc.) working in the `powerpacks` repo.

`CLAUDE.md`, `.cursorrules`, etc. are symlinks to this file. If they drift,
re-run `bin/sync-agent-files.sh` from the repo root.

---

## Sub-agent delegation

The user explicitly authorizes Codex to use sub-agents for this repo. If skills
request sub-agents, use them. Leverage sub-agents to keep the main conversation
clean and concise.

Sub-agents are a finite resource. When a sub-agent reaches a terminal result
(`completed`, failed, or no longer needed after a blocker is reported), close it
with `close_agent` before ending the turn. If spawning fails because the
sub-agent pool is full, inspect the existing sub-agents, close stale completed
ones, then retry the intended delegation before falling back to running noisy or
long-lived work in the main thread.

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
- **Indexing pack** (build-local-search-index): local files only. It consumes
  `.powerpacks/network-import/merged/people.csv` and writes
  `.powerpacks/search-index/`; do not run LLM, network, Supabase,
  Postgres, or TurboPuffer calls for this workflow.
- **Search pack** (search-network, search-company): requires `.env` with
  TurboPuffer + Postgres credentials. If `.env` is present, run the search
  primitive directly and use its error to diagnose; use the doctor only if env
  or auth looks broken and the cause is unclear. For `$search-network`, after
  loading `packs/search/skills/search-network/SKILL.md`, use its documented
  company-directory MCP fast path for company-only people lookups; otherwise
  start with `search_network_pipeline.py prepare --query ...`. Do not
  grep/search/read search docs, schemas, primitive source, or prior artifacts on
  the happy path.

Don't run pack-specific checks pre-emptively. Only when the user's request
implies that pack.

---

## Skill routing

When a user request matches a Powerpacks skill, load that skill's `SKILL.md` and
follow it. This section only routes intent to skills; it does not define skill
internals, primitive sequences, or orchestration details.

Routes:

- `$search-network`, people search, network search, role/title/location/school
  searches, "who is...", "find people...", company-directory queries →
  `packs/search/skills/search-network/SKILL.md`
- `$search-company`, company lookup, company IDs, investor/funding/sector or
  company-set resolution → `packs/search/skills/search-company/SKILL.md`
- `$search-contacts`, my contacts, set contacts, contact field filtering →
  `packs/contacts/skills/search-contacts/SKILL.md`
- `$build-local-search-index`, local indexing, build local search index,
  prepare `.powerpacks/search-index` artifacts →
  `packs/indexing/skills/build-local-search-index/SKILL.md`
- `$setup`, one-time setup, operator bootstrap restore, end-to-end local
  ingestion setup, account/source linking plus import/index orchestration →
  `packs/ingestion/skills/setup/SKILL.md`
- `$sales-nav-search`, Sales Navigator leads, LinkedIn lead searches →
  `packs/sales-nav/skills/sales-nav-search/SKILL.md`
- `$powerset`, `$powerset setup`, Powerset login/status/whoami/sets/MCP/env credentials →
  `packs/powerset/skills/powerset/SKILL.md`
- `$import-contacts`, iMessage, WhatsApp, contact import/review/upload/retarget →
  `packs/messages/skills/import-contacts/SKILL.md`
- `$msgvault`, `$local-msg-vault`, `msgvault setup`, `powerset create oauth app`, Gmail OAuth app setup for msgvault →
  `packs/ingestion/skills/msgvault/SKILL.md`
- `$onboard`, `$ingestion-onboarding`, local ingestion onboarding, link/export
  local network sources →
  `packs/ingestion/skills/onboard/SKILL.md`
- `$import-email`, Gmail, email, msgvault metadata import →
  `packs/ingestion/skills/import-email/SKILL.md`
- `$import-twitter`, Twitter/X network import or Twitter/X smoke test →
  `packs/ingestion/skills/import-twitter/SKILL.md`
- `$import-network`, local network ingestion orchestration, LinkedIn CSV plus
  msgvault/messages/Twitter merge, DuckDB materialization →
  `packs/ingestion/skills/import-network/SKILL.md`

Do not ask the user to pick a skill when the route is obvious. Ask a brief
clarifying question only when the same wording could mean multiple surfaces,
for example "people at OpenAI" versus "contacts at OpenAI".

Once a route is obvious, avoid exploratory repo reads. The skill's executable
primitive is the source of truth for normal operation; inspect files only when
the primitive blocks/fails or the user asks for implementation details.

---

## General defaults

- **Be terse on operational status.** Print one-line summaries of what
  primitives wrote / what counts came back. Do not narrate the whole plan.
- **Don't ask permission for read-only operations** (TurboPuffer filter
  searches, local file reads, scoped `check`/`status` commands, `whoami`,
  `estimate` subcommands, and doctor `run` when it is actually needed by the
  health-check policy). Ask only for spend (LLM calls, Parallel.ai submits,
  uploads, Docker pulls, browser-based logins, OS installs).
- Prefer small, inspectable primitives. Dependencies are allowed when they make
  product paths safer or clearer; add them through project metadata and run via
  `uv run --project . ...` so agents use the locked environment.
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
│   ├── indexing/               # build-local-search-index local artifacts
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
