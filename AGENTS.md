# Powerpacks agent guidance

## GitHub PR tooling

If you are running on Vorflux (Vorflux PR tools or the `vflux` CLI are available), use them for PR creation, editing, commenting, reviewing, and merging. Prefer the Vorflux GitHub App identity; only use a connected personal account when the user explicitly asks for personal attribution, and confirm the token path via the tool's `used_user_token` indicator.

Otherwise (local sessions, no Vorflux tooling), use `gh` directly. Before mutating PR actions, run `gh api user --jq .login` to verify the active account is the intended identity, and mention the verified login in your status update.

## Vorflux PR body checklist guardrail

When a Vorflux session touches multiple repositories, every non-docs PR body must include the exact `## Cross-Repo Ship Checklist` section before the PR is considered ready. Add it at PR creation time with the Vorflux PR tool body/body-file; if it is missing or stale, update the PR body with `vflux pr edit` instead of pushing a no-op commit.

Required checklist fields:
- `**Touched repos:**` - comma-separated `org/repo` slugs for every repo changed in the session.
- `**Companion PRs:**` - links to the other PRs in the session, or `N/A` only when a single repo was touched.
- `**Production deploy plan:**` - what will be deployed/merged and in what order, or why no production deploy is required.
- `**Post-deploy verification plan:**` - exact health, workflow, UI, or artifact checks that prove the shipped change is live.

If a repo has a `scripts/cross_repo_ship.py` helper, prefer using its `prepare-pr` command to generate/update this section. Before finalizing, verify the latest edited PR-body-triggered checklist run passes when the repo has a Cross-Repo Ship Checklist workflow; do not treat an older failed run as current after the body has been fixed.

## Release Please / Conventional Commit guidance

Powerpacks uses `googleapis/release-please-action` on `main`. Do not manually push version tags for normal releases. Land conventional commits on `main`; Release Please opens or updates a release PR; merging that release PR creates the GitHub release and component tag.

Releasable commit shapes for this repo:
- `fix: ...` - patch release.
- `feat: ...` - minor release, e.g. `0.1.0` -> `0.2.0`.
- `feat!: ...`, `fix!: ...`, `refactor!: ...`, or any conventional commit with a `BREAKING CHANGE:` footer - major release.
- `deps: ...` - Release Please treats dependency updates as releasable.
- `docs: ...` - can be releasable for Java/Python release types; avoid relying on docs-only commits when you need a guaranteed minor/major bump.

Usually non-releasable unless breaking: `chore: ...`, `ci: ...`, `build: ...`, `test: ...`, `refactor: ...`, `style: ...`.

This repo has two Release Please packages:
- `.` as Python package `powerpacks`, tagged like `powerpacks-vX.Y.Z`.
- `app` as Node package `powerpacks-console`, tagged like `powerpacks-console-vX.Y.Z`.

Commits whose changed files are under `app/` can affect the console component; root/package commits affect the `powerpacks` component. A root-only docs/guidance PR should release only `powerpacks`; `powerpacks-console` needs an `app/` change or app-scoped releasable commit to get its own release PR/tag. If one human-facing change should release both components, make sure the merged PR has meaningful changes in both paths and uses a releasable conventional commit.

To intentionally cut a Powerpacks minor release such as `0.2.0`, merge a PR with a `feat: ...` commit/message after the release-please setup is on `main` (for example `feat: document Powerpacks 0.2.0 pipeline release`). To intentionally cut a major release, use `feat!: ...` or include a `BREAKING CHANGE:` footer. After that commit lands, wait for the `release-please` workflow to open/update the release PR, review the generated changelog/version bumps, then merge the release PR.

Manual release escape hatch: run the Release Please workflow/CLI with an appropriately scoped token only if automation is blocked. Prefer the normal release PR flow so versions, changelogs, manifests, and tags stay consistent.

This file is the canonical bootup instruction sheet for any coding agent
(Codex, Claude Code, NanoClaw, pi, etc.) working in the `powerpacks` repo.

`CLAUDE.md`, `.cursorrules`, etc. are symlinks to this file. If they drift,
re-run `bin/sync-agent-files.sh` from the repo root.

---

## Reference fidelity

Do not be lazy when the user asks to copy, mirror, reference, or base work on an
existing implementation. For frontend work, copy the referenced style, layout,
component choices, spacing, colors, and code structure as close to 1:1 as
possible unless underlying functionality makes that impossible. For backend
work, copy the same interface, logic, and behavior as closely as possible unless
the user explicitly asks not to copy it or asks for a different approach.

---

## Data pipeline simplicity (do not overengineer)

This is a small, local, file-based data pipeline. Keep it that way. Most
"robustness" features you might reach for are over-engineering here and are
explicitly unwanted.

Hard rules for any ingestion/discovery/enrichment/indexing change:

- **No ledgers.** Do not add `*-ledger.json`, step ledgers, or per-step
  state machines. A stage writes its output files plus one `manifest.json`
  in its own directory. That is the entire state contract. (Existing legacy
  ledgers are being phased out — do not add more or extend them.)
- **No run ids or batch ids.** No per-run UUIDs, no batch/job identifiers, no
  run-scoped subdirectories. Each stage writes to a single, fixed directory
  (e.g. `.powerpacks/network-import/discover/gmail/`) and overwrites in place.
  Reruns are idempotent because the output path is stable, not because of an id.
- **Manifest + outputs only.** The durable artifacts a stage produces are its
  output CSV/JSONL files and `manifest.json` (use the existing
  `write_manifest` in `import_contacts_pipeline/common.py`). Counts, status,
  timestamps, and timing go in the manifest — not a separate state store.
- **Progress goes in a file, like LinkedIn.** For user-facing progress, write
  human-readable progress into the stage's manifest/output directory and let the
  FE render it the same way the LinkedIn flow does. Do not invent a new progress
  store or a parallel event stream the FE doesn't already read.
- **Lean on what's already solved; don't rebuild it.** Resumability,
  incrementality, and dedup mostly already exist. Gmail/msgvault is already
  resumable: compute the latest synced message, pass `--after`, sync, update —
  see `infer_msgvault_sync_after` in
  `discover_contacts_pipeline/gmail.py`. Do not build a new resume mechanism on
  top of it.
- **Orchestrate the existing primitives directly; do not route new flows
  through `setup/setup.py`.** Chain the existing `discover_contacts_pipeline`
  and `import_contacts_pipeline/<source>.py` commands. `setup.py` is not the
  orchestration layer for new pipeline flows.
- **Do not fingerprint the shared `directory.csv`.**
  `.powerpacks/network-import/directory.csv` is a cross-source aggregate, not a
  source-owned output. Treating it as a per-source fingerprint makes restored
  imports look stale and re-runs cached enrichment.

When in doubt, do the smaller thing: fewer files, fewer concepts, one
directory, one manifest. If you think a change genuinely needs more machinery
than this, stop and ask the user before building it.

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

Never run `fix` unprompted. Browser logins, Google Cloud auth for msgvault/Gmail OAuth setup, Full Disk Access,
Docker/WAHA QR setup, and spend-bearing operations are user-consent operations.
Python dependency setup via `bin/setup-python` is part of normal onboarding.

For Powerset users, runtime keys come from the authenticated Powerset API via
`packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py`. The normal
Powerset setup path is Auth0 plus API key pull plus MCP registration. If
runtime keys are missing after `$powerset env pull`, the likely blocker is
Powerset-side provisioning of the user's Modal/OpenAI runtime keys. Google Cloud
CLI login is still a valid user-consent flow for msgvault/Gmail OAuth app setup
only; keep that scoped to the msgvault primitives.

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
- **Search pack** (search-network, search-profile, search-company):
  `$search-network` and `$search-company` require `.env` with TurboPuffer +
  Postgres credentials. If `.env` is present, run the search
  primitive directly and use its error to diagnose; use the doctor only if env or
  auth looks broken and the cause is unclear. For `$search-network`, after
  loading `packs/search/skills/search-network/SKILL.md`, use the
  `search_network_pipeline.py prepare --query ...` path for ordinary people
  searches and company-only lookups; the primitive owns company-directory fast
  path detection. For job posting URLs, pasted JDs, or broad role briefs, load
  `packs/search/skills/recruit/SKILL.md` (`$recruit` supersedes the deprecated
  `$search-profile`; a job URL runs through `recruit_loop.py --jd-url`). Do not
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

- `$search-network`, people search, network search, local network search,
  role/title/location/school searches, "who is...", "find people...",
  company-directory queries →
  `packs/search/skills/search-network/SKILL.md`
  (routes internally between local DuckDB, TurboPuffer, and profile modes)
- `$recruit`, emulate a recruiting team end-to-end for a JD — **job posting
  URLs** (via `recruit_loop.py --jd-url`), **pasted job descriptions**, and
  **complex role briefs** all route here — shotgun many small archetype searches
  + mixture-of-judges shortlist + core-gate + expand-from-anchor + epoch
  convergence vs a judged ground-truth set (supersedes the deleted
  search-highlight) → `packs/search/skills/recruit/SKILL.md`
- `$search-profile` — **deprecated alias of `$recruit`.** Job-posting URLs,
  pasted JDs, complex role briefs, and recruiter plan-and-execute are now
  handled by `$recruit`, which does the same JD→judged-shortlist job with a
  core-tagged plan, mixture-of-judges, core-gate, and IC-track-aware seniority.
  Route these to `$recruit`; `packs/search/skills/search-profile/SKILL.md` still
  works for back-compat but new work should use `$recruit`.
- `$search-company`, company lookup, company IDs, investor/funding/sector or
  company-set resolution → `packs/search/skills/search-company/SKILL.md`
- `$search-sql`, relational/aggregate local people queries ("who overlapped
  with X at a company", "2+ startup stints", career-shape predicates),
  read-only SQL over the local search DuckDB →
  `packs/search/skills/search-sql/SKILL.md`
- `$search-contacts`, my contacts, set contacts, contact field filtering →
  `packs/contacts/skills/search-contacts/SKILL.md`
- `$build-local-search-index`, local indexing, build local search index,
  prepare `.powerpacks/search-index` artifacts →
  `packs/indexing/skills/build-local-search-index/SKILL.md`
- `$setup`, one-time LinkedIn-only setup: import LinkedIn Connections.csv +
  fan-in merge + Modal index + validate (for Gmail use `$import-gmail`; for
  iMessage/WhatsApp use `$import-messages`) →
  `packs/ingestion/skills/setup/SKILL.md`
- `$sales-nav-search`, Sales Navigator leads, LinkedIn lead searches →
  `packs/sales-nav/skills/sales-nav-search/SKILL.md`
- `$build-outbound`, Apollo setup/status, Sales Nav outbound copy preview, inactive Apollo sequence/contact build, separate campaign activation →
  `packs/apollo/skills/build-outbound/SKILL.md`
- `$powerset`, `$powerset setup`, Powerset login/status/whoami/sets/MCP/env credentials →
  `packs/powerset/skills/powerset/SKILL.md`
- `$update-powerpacks`, reinstall/update Powerpacks skills, canonical install/state cleanup, adopt `.codex` state →
  `packs/powerset/skills/update-powerpacks/SKILL.md`
- `$fix-powerpacks`, diagnose/fix local Powerpacks state paths, copy newer `.powerpacks` state into canonical repo, validate linked source wiring →
  `packs/powerset/skills/fix-powerpacks/SKILL.md`
- `$import-messages`, iMessage/WhatsApp, message-contact local import (discover →
  match vs LinkedIn/Gmail → deep-research → mandatory review → import) + fan-in
  merge + Modal index; local only, never uploads to Powerset →
  `packs/messages/skills/import-messages/SKILL.md`
- `$msgvault`, `$local-msg-vault`, `msgvault setup`, `powerset create oauth app`, Gmail OAuth app setup for msgvault →
  `packs/ingestion/skills/msgvault/SKILL.md`
- `$onboard`, `$ingestion-onboarding`, local ingestion onboarding, link/export
  local network sources →
  `packs/ingestion/skills/onboard/SKILL.md`
- `$import-gmail`, Gmail, email, msgvault setup + sync + import + fan-in merge +
  Modal index →
  `packs/ingestion/skills/import-gmail/SKILL.md`
- `$enrich-email-markers`, gmail LLM enrichment, mine email bodies for LinkedIn
  markers, preview the context/markers we'd send to an LLM →
  `packs/ingestion/skills/enrich-email-markers/SKILL.md`
- `$deep-context`, build deep context, per-person dossier from message bodies
  (Gmail + iMessage/WhatsApp DMs), "context/dossier on a person", "who is
  <phone/name> in my messages", find same-person/merge candidates →
  `packs/ingestion/skills/deep-context/SKILL.md`
- `$logbook`, raw verbatim message archive from a people CSV, "build a logbook",
  "archive/dump everything I've said with these people", "every email/text/whatsapp
  with <person> verbatim", append-only incremental sync (no LLM, no spend) →
  `packs/ingestion/skills/logbook/SKILL.md`
- `$import-twitter`, Twitter/X network import or Twitter/X smoke test →
  `packs/ingestion/skills/import-twitter/SKILL.md`
- `$discover-contacts`, local network ingestion orchestration, LinkedIn CSV plus
  msgvault/messages/Twitter merge, DuckDB materialization →
  `packs/ingestion/skills/discover-contacts/SKILL.md`

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
- **Apollo outbound safety**: `$build-outbound` uses `packs/apollo/primitives/build_outbound/build_outbound.py` for Sales Nav resolution, preview, inactive sequence/contact builds, and exact-id activation. Preview/resolve are safe; enrichment, contact writes, sequence/campaign creation, enrollment, and activation are spend-bearing or mutating. Require explicit confirmation before build unless the user clearly asked to build now, and require exact `campaign_id` confirmation before activation. Do not run live activation tests.
- **Privacy contract**: no message bodies are ever read or sent. Only
  contact metadata (phone, name, source, group flags, message counts,
  last_message). Carry this through any new primitive. **Scoped exception:** the
  `$deep-context` skill reads message bodies (Gmail + iMessage/WhatsApp DMs) to
  build per-person dossiers — deep body inspection is its whole purpose. DMs only;
  group bodies are never read; raw bodies are ephemeral/gitignored; dossiers store
  synthesized facts, not verbatim text. **Second scoped exception:** the
  `$logbook` skill reads AND persists **verbatim** bodies — Gmail threads,
  iMessage/WhatsApp DMs, and (with `--include-groups`) group chats — to build a
  raw conversation archive; verbatim persistence including group bodies is its
  whole purpose. Output lives gitignored under `.powerpacks/logbook/`; nothing is
  sent anywhere (no LLM, no network). These two skills are the ONLY exceptions —
  every other primitive stays metadata-only.
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
