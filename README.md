# Powerpacks

<!-- Changelog: 2026-07-10 — distinguish the install URL from the Powerset provisioning API. -->

`powerpacks` is a portable bundle of skills + deterministic primitives +
check-in data contracts that turn a coding-agent host (Codex, Claude Code,
Pi, NanoClaw) into a recruiting-search and contact-import workstation backed by
Powerset.

The core package is host-agnostic. The same skills run unchanged across hosts;
only the install adapter differs.

## Install

Tell your agent (Codex, Claude Code, ...) one sentence:

> Download and install the Powerpacks skill from
> https://powerset.dev/powerpacks

Then just say what you want — the skill clones this repo, installs everything
for your harness, and keeps going in the same session:

> Use Powerpacks to set up my local network search using my Powerset account.

That initializes the public hosted config, signs in to Powerset, pulls the
provisioned Modal/OpenAI runtime keys for that user, and then builds the local
LinkedIn network index. The shorter `Use Powerpacks to set up my local network
search` prompt remains supported.

### Other install paths

For Codex, let Codex fetch/update the repo and run the installer:

```bash
codex exec "Clone https://github.com/powerset-co/powerpacks if needed, then cd into powerpacks and run bin/update-codex."
```

For Claude Code:

```bash
claude -p "Clone https://github.com/powerset-co/powerpacks if needed, then cd into powerpacks and run bin/update-claude-code."
```

For direct/manual installs, run the adapter install for your harness:

```bash
./install.sh codex
```

### Docker heartbeat worker

For a long-running Codex heartbeat under Docker, run:

```bash
scripts/run-codex-heartbeat-docker.sh start
```

The worker mounts this checkout, installs/refreshes Powerpacks inside the
container, and loops on `codex exec` with Docker `--restart unless-stopped`.
By default it shares the host Codex login safely by mounting
`${CODEX_HOME:-$HOME/.codex}` read-only and copying that snapshot into a
separate writable container volume. See
[`docs/codex-heartbeat-docker.md`](docs/codex-heartbeat-docker.md).

The install flow runs `bin/setup-python`, which installs `uv` on macOS when
Homebrew is available, then installs Python project dependencies from
`pyproject.toml` / `uv.lock`.

For the local Powerpacks Console app, use npm through the repo installer:

```bash
./install.sh app
./install.sh app --dev --port 5177
```

Direct app commands are also supported:

```bash
cd app
npm install
npm run dev
# Explicit sharing/preview only:
npm run dev:share
```

## Skills

User-facing skill entrypoints, grouped by purpose. Each skill ships its own
`SKILL.md` with the full workflow.

Product and architecture walkthroughs live in the
[`docs` hub](docs/README.md). GitHub renders the Mermaid diagrams directly.

### Search

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`search`](packs/search/skills/search/SKILL.md) | `$search <query-or-jd>` | The single people-search door. The agent records a Step-1 decision (`decision.json`: surface / backend / depth) and dispatches. Ordinary queries: one expansion → hybrid retrieval (TurboPuffer+Postgres or local DuckDB) → LLM filter/rerank, behind one confirm gate. A JD / job-posting URL / role brief runs **deep mode**: resolve the recruiter contract → automated critic → one human Review → wide source → conservative triage → one selected judge → core-gated shortlist → expand-from-anchor until converged. Deep sourcing supports both Powerset and local DuckDB; the in-loop SQL lane and automated judge panel are planned. See the [search architecture](packs/search/docs/search-architecture.md). |
| [`search-company`](packs/search/skills/search-company/SKILL.md) | `$search-company <query>` | Resolves company names, descriptions, sectors, investor/funding filters into canonical TurboPuffer company IDs. |
| [`search-sql`](packs/search/skills/search-sql/SKILL.md) | `$search-sql <question>` | Agentic read-only SQL over the local search DuckDB, for relational/aggregate people queries the filter DSL can't express (overlap joins, per-person aggregates, career-shape predicates). |
| [`build-local-search-index`](packs/indexing/skills/build-local-search-index/SKILL.md) | `$build-local-search-index` | Builds the fixed local search index at `.powerpacks/search-index/local-search.duckdb` from the canonical merged people CSV without Modal, Postgres, or TurboPuffer. Planning is local-only; full builds may use configured providers for cache misses. |

### Setup and ingestion

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`powerset`](packs/powerset/skills/powerset/SKILL.md) | `$powerset setup`, `$powerset login`, `$powerset status`, `$powerset sets ...` | Unified Powerset command surface: one-command setup (Auth0 login + Powerset API runtime-key pull + MCP), credential refresh, setup status, Auth0 identity, MCP install, and local default set selection. `$powerset-login` / `$powerset-set` remain aliases. |
| [`setup`](packs/ingestion/skills/setup/SKILL.md) | `$setup` | Deterministic LinkedIn-only setup: credentials, Modal profile enrichment, local source merge, Modal indexing, DuckDB download, and read-only validation. Gmail and messages remain separate import skills. |
| [`msgvault`](packs/ingestion/skills/msgvault/SKILL.md) | `$msgvault`, `$local-msg-vault`, `$powerset create oauth app` | Guided msgvault setup for local Gmail archive access: install/status, browser-assisted Google OAuth Desktop app creation, client secret config, account auth, and Codex MCP registration. |
| [`import-gmail`](packs/ingestion/skills/import-gmail/SKILL.md) | `$import-gmail` | Contact sync only: bounded msgvault sync, metadata-only extraction, free local-directory attach, and source fan-in. Unresolved contacts land in a research-candidates pool; identity lookups and indexing happen in `$deep-context`. See the [product guide](packs/ingestion/docs/gmail-import-pipeline.md). |
| [`import-messages`](packs/ingestion/skills/import-messages/SKILL.md) | `$import-messages` | Contact sync only: adds iMessage/WhatsApp contact metadata, matches against imported LinkedIn/Gmail people for free, imports matched people + a research-candidates pool, and merges sources. No LLM, no research, no index build in-skill. See the [product guide](packs/ingestion/docs/message-import-pipeline.md). |
| [`import-whatsapp`](packs/ingestion/skills/import-whatsapp/SKILL.md) | `$import-whatsapp` | Isolated WhatsApp metadata sync/export using `wacli`; the [message import guide](packs/ingestion/docs/message-import-pipeline.md#isolated-import-whatsapp) explains where it stops. |
| [`deep-context`](packs/ingestion/skills/deep-context/SKILL.md) | `$deep-context`, "process my contacts" | The single post-import workflow and dossier surface: context over people + unresolved candidates, duplicate merge, a binary exception queue with editable Added/Rejected piles, one budget-gated lookup for Added people, binary LinkedIn review, synthetic profiles, fan-in + Modal index + validate, plus ad-hoc person lookup/re-review. See the [product guide](packs/ingestion/docs/deep-context-pipeline.md). |

### Sales Nav

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`sales-nav-search`](packs/sales-nav/skills/sales-nav-search/SKILL.md) | `$sales-nav-search` | Run a Sales Navigator search through the `powerset-search` MCP. Resolves company / title filters, runs a paginated lead search with server-side artifact persistence on by default, paginates via `get_artifact`. Depends on `$powerset setup` having run first. |
| [`build-outbound`](packs/apollo/skills/build-outbound/SKILL.md) | `$build-outbound setup`, `$build-outbound <instructions>` | Resolve Sales Nav leads, preview copy, enrich through Apollo, create an inactive Apollo sequence/campaign, enroll contacts, and activate only with a separate exact campaign confirmation. |

## Goal

- make TurboPuffer and Postgres contracts explicit enough that agents do not
  guess field names, operators, or value types
- give the agent operational entrypoints: `$search <query-or-jd>`,
  `$search-company <query>`, `$powerset setup`, and the message import
  skill
- interpret standard queries and decompose deep recruiting roles into bounded
  candidate-archetype probes
- persist auditable plans, provenance, evaluations, and result artifacts
- keep host-specific runtime glue isolated under `adapters/`

## Layout

**Everything is a pack.** No more top-level `skills/`, `primitives/`,
`schemas/`, `contracts/`, `tasks/`, or `evals/`. Each domain pack is a
self-contained slice of the system.

```text
powerpacks/
├── packs/
│   ├── powerset/           identity + runtime env + MCP install
│   │   │                   (depended on by every other pack)
│   │   ├── skills/         powerset (unified commands), powerset-login,
│   │   │                   powerset-set (backcompat aliases)
│   │   ├── primitives/     auth/ (Auth0 PKCE),
│   │   │                   pull_runtime_keys/ (Powerset API key pull),
│   │   │                   doctor/ (one-shot setup health check),
│   │   │                   mcp_install/ (powerset-search MCP into
│   │   │                                 Claude Code / Codex)
│   │   └── templates/      env.example
│   ├── sales-nav/          Sales Navigator search via the powerset-search MCP
│   │   └── skills/         sales-nav-search
│   ├── apollo/             Apollo.io setup + script-backed outbound builds
│   │   ├── skills/         build-outbound
│   │   └── primitives/     apollo_mcp, build_outbound
│   ├── indexing/           local people.csv → search-index artifact pipeline
│   │   ├── skills/         build-local-search-index
│   │   ├── primitives/     build_processing_pipeline + transform CLIs
│   │   ├── lib/            contracts, identity, people/artifact builders
│   │   └── tasks/          build-local-search-index.task.json
│   ├── ingestion/          LinkedIn, Gmail, Twitter, iMessage, and WhatsApp imports
│   │   ├── skills/         setup, import-gmail, import-messages,
│   │   │                   import-whatsapp, deep-context
│   │   ├── primitives/     source discovery/import + message leaf primitives
│   │   ├── schemas/        people + message-contact contracts
│   │   └── docs/           maintained ingestion product guides
│   ├── search/             recruiting people / company search
│   │   ├── skills/         search, search-company
│   │   ├── primitives/     search CLIs + deep orchestration + shared lib/
│   │   │                   + task_state/
│   │   ├── schemas/        decomposed-query, role-search-filters,
│   │   │                   task-run.schema.json, etc.
│   │   ├── contracts/      checked-in Postgres + TurboPuffer schemas
│   │   ├── tasks/          compatibility and evaluation task specifications
│   │   ├── docs/           canonical architecture, backend contracts,
│   │   │                   method, and benchmark evidence
│   │   └── evals/          recall, company-search, founder parity
├── adapters/               codex/, claude-code/, pi/, nanoclaw/ installers
├── docs/                   cross-pack docs (quickstart.md, testing.md)
├── scripts/                test-powerpacks, lint-powerpacks
├── tests/                  cross-pack test suite
└── templates/              host-install templates (claude-fragments,
                            container.json)
```

The `powerset` pack is the foundation — every other pack depends on its
`auth` and `task_state` primitives. Anyone using Powerpacks runs
`$powerset setup` first; `$powerset login` remains available as a backcompat
credential refresh command.

## Quickstart for a fresh account

Use this path for a new Codex, Claude Code, or Pi setup. A fuller walkthrough is in
[`docs/quickstart.md`](docs/quickstart.md).

```bash
# 1. Let Codex clone/update the repo and run its install adapter.
codex exec "Clone or update https://github.com/powerset-co/powerpacks in the current directory, then run the Codex install and Powerset login/MCP setup steps from its instructions."

# Or install manually from a local checkout.
git clone git@github.com:powerset-co/powerpacks.git
cd powerpacks
./install.sh codex
# or: ./install.sh claude-code
# or: ./install.sh pi

# 2. Install/auth the Powerset MCP for MCP-backed skills.
# This starts Auth0 login if needed and writes the bearer token into host config.
./install-powerset-mcp.sh --host codex
# or: ./install-powerset-mcp.sh --host claude

# 3. Verify MCP config.
codex mcp get powerset-search
# Expected for Codex:
#   bearer_token_env_var: -
#   http_headers: Authorization=*****

# 4. Restart the agent host so it reloads skills and MCP config.

# 5. Inside the agent, run what you need:
$search senior infra eng at fintech
$search-company stripe-like fintech infra companies
$powerset setup                   # login + .env pull + powerset-search MCP
$import-gmail                     # Gmail contact sync (free after OAuth)
$import-messages                  # iMessage + WhatsApp contact sync
$import-whatsapp                  # isolated WhatsApp sync test via wacli
$deep-context                     # process contacts end-to-end, or run ad-hoc
                                  #   dossier lookup/re-review subcommands
```

### Prereqs by skill family

| You want to use… | Install on the host running Codex / Claude Code / Pi |
| --- | --- |
| Any skill | `uv`, git. Powerpacks uses uv-managed Python 3.12 from `.python-version`. |
| `search` / `search-company` | `.env` populated with Powerpacks runtime secrets; see [Secrets / env vars](#secrets--env-vars). |
| `powerset setup` | Powerset/Auth0 account. Runtime keys are pulled from the Powerset API when provisioned. |
| `import-gmail` | msgvault/Gmail OAuth only — the import itself is free and local (identity lookups moved to `deep-context`). |
| `import-messages` | macOS Full Disk Access for iMessage and WhatsApp/wacli setup — no LLM or research secrets needed in-skill. |
| `deep-context` | A merged network + linked message sources, Full Disk Access for iMessage, OpenAI for dossiers/triage, Parallel for the approved-person lookup, RapidAPI for retarget hydration, and Modal runtime keys for the index rebuild. |
| `sales-nav-search` | `$powerset setup` already run (it ships the Auth0 token + registers the `powerset-search` MCP into your host) |
| `build-outbound` | `APOLLO_API_KEY` in `.env` or shell, connected Apollo email account/schedule, and a Sales Nav run or manifest. Node/npx is only needed for MCP setup/status. |

### Secrets / env vars

`$powerset setup` is the recommended one-command path: it logs in, runs
`$powerset env pull`, and installs/refreshes the `powerset-search` MCP.
`$powerset login` and `$powerset env pull` remain available as smaller
backcompat/maintenance commands. Env pull populates `.env` from the
authenticated Powerset API for provisioned users. Hosted processing for
Powerset users runs on Modal.

| Key | Used by |
| --- | --- |
| `TURBOPUFFER_API_KEY` | Search retrieval |
| `DATABASE_URL` | Search hydration, prefilters, metadata |
| `OPENAI_API_KEY` | Query extraction, LLM filtering/reranking |
| `OPENROUTER_API_KEY` | Messages contact review |
| `PARALLEL_API_KEY` | Messages deep research |
| `POWERPACKS_DEFAULT_SET_ID` | Local default Powerset set selection |
| `APOLLO_API_KEY` | Apollo.io outbound build; use a Master API key for sequence/campaign, email-account, schedule, enrichment, and contact endpoints |

Additional provider keys can still be supplied manually for workflows that run
locally outside the provisioned Powerset API path.

## Install

The top-level `install.sh` dispatches to a per-host adapter. **All adapters
are idempotent — re-run them any time skills change** (you do not need
to uninstall first; each adapter wipes and re-copies the skill directories).

### Codex

```bash
codex exec "Clone https://github.com/powerset-co/powerpacks if needed, then cd into powerpacks and run bin/update-codex."

# Manual equivalent from a local checkout:
bin/update-codex                           # pull, sync agent files, reinstall Codex skills/profile
./install.sh codex                          # default: ~/.codex/skills/
./install.sh codex /custom/skills/dir       # explicit target
```

The Codex adapter installs each skill entrypoint under `~/.codex/skills/<skill>/`
and stores one shared support bundle at `~/.codex/powerpacks`. Each installed
skill links `powerpacks/` to that shared bundle, so adding a skill does not copy
the full pack tree again.

### Claude Code

```bash
claude -p "Clone https://github.com/powerset-co/powerpacks if needed, then cd into powerpacks and run bin/update-claude-code."

# Manual equivalent from a local checkout:
bin/update-claude-code                    # pull, sync agent files, reinstall Claude Code skills
bin/update-claude                         # alias for update-claude-code
./install.sh claude-code                    # default: ~/.claude/skills/
./install.sh claude-code ./.claude/skills   # project-level install
```

### Pi

```bash
./install.sh pi                             # default: ~/.pi/agent/skills/
./install.sh pi ./.pi/skills                # project-level install
```

Pi discovers skills from `~/.pi/agent/skills/`, `.pi/skills/`, `.agents/skills/`,
and configured package/settings paths. Installed skills are available as
`/skill:<name>` commands (for example `/skill:powerset whoami`) and can also be
loaded naturally when you type prompts like `$powerset whoami`. Pi does not ship
first-party MCP support, so the Pi adapter installs skills only; MCP-backed
Powerpacks flows still need a Pi MCP extension or a host with MCP support.

### NanoClaw

```bash
./install.sh nanoclaw /path/to/nanoclaw
```

Restart the agent host after install so it reloads the skill list. Direct
adapter installs also work:

```bash
./adapters/codex/install.sh                    [skills-dir]
./adapters/claude-code/install.sh               [skills-dir]
./adapters/pi/install.sh                         [skills-dir]
./adapters/nanoclaw/install.sh /path/to/nanoclaw
```

The NanoClaw adapter copies the core Powerpacks directory into the target,
installs `search`, wires the
threaded CLI channel, and keeps NanoClaw-specific TUI/runtime code under
`powerpacks/adapters/nanoclaw`.

### Reinstall after pulling new changes

```bash
cd powerpacks
bin/update-codex            # Codex: pull, sync agent files, reinstall skills/profile
bin/update-claude-code       # Claude Code: pull, sync agent files, reinstall skills
./install.sh pi              # or nanoclaw <path>
# then restart the agent host, or run /reload in Pi, so it re-reads the skill list
```

This is the only command needed for skill / primitive changes. The `mcp_install`
registrations are written to host config files (`~/.codex/config.toml`,
`~/.claude.json`) and only need re-running when the MCP URL or token format
changes — `$powerset setup` covers that path.

### MCP install (powerset-search)

The `sales-nav-search` skill and any future MCP-driven skills need the remote
`powerset-search` MCP registered with your agent host. The wrapper below
handles Auth0 login when credentials are missing or expired, installs the MCP,
and writes the bearer token into host config:

```bash
./install-powerset-mcp.sh --host codex      # or claude/all
# verify
claude mcp list                  # for Claude Code
codex mcp list 2>/dev/null \
  || uv run --project . python packs/powerset/primitives/mcp_install/mcp_install.py status --host codex
```

Claude Code bakes the bearer token into `~/.claude.json` at install time.
Codex stores the bearer header in `~/.codex/config.toml`, matching Codex's
HTTP MCP config shape. Re-run the installer to refresh the token:

```bash
./install-powerset-mcp.sh --host codex
```

## Verify your install

Quick checks that each layer works — run from the repo root after
`./install.sh <host>`:

```bash
# 1. Skill files actually copied to the host
ls ~/.codex/skills/                # or ~/.claude/skills/, ~/.pi/agent/skills/

# 2. Powerpacks unit tests
python3 -m unittest discover -s tests

# 3. MCP reachability (after $powerset setup)
claude mcp list                    # "powerset-search ... ✓ Connected"
uv run --project . python packs/powerset/primitives/doctor/doctor.py run
```

Then, **inside the agent host**, sanity-check each skill family:

| Skill | Test prompt |
| --- | --- |
| `powerset setup` | Type `$powerset setup` — the agent should run the doctor, handle missing login, pull runtime keys, and finish with `mcp_install`. |
| `search` | `$search senior infra engineers in NYC` — should produce a plan + approval prompt, not retrieve anything yet. |
| `sales-nav-search` | `$sales-nav-search VPs of engineering at Stripe` — should resolve company id, run the search, return a first page of leads + an `artifact_id`. |
| `import-gmail` | `$import-gmail` — should create a nine-step plan, ask for accounts/window, use one bounded multi-account discovery command, and end by suggesting missing sources + offering `$deep-context`. |
| `import-messages` | `$import-messages` — should show a seven-step checklist, run free through discover/match/import (one import confirmation), and end with the same suggest/process tail. |
| `import-whatsapp` | `$import-whatsapp` — should install/find wacli, show QR if needed, sync once, and export WhatsApp metadata. |
| `deep-context` | `$deep-context check` should report local source readiness without starting a paid build; bare `$deep-context` should begin with sources, people, and waiting candidates. |

If the agent host doesn't see a skill at all: re-run `./install.sh <host>`
and restart the host (skills are loaded once at startup).



## Contracts

Powerpacks treats Postgres and TurboPuffer schema as checked-in contracts, not
something the agent should rediscover on each run:

```bash
uv run --project . python packs/search/primitives/contracts/contracts.py list
uv run --project . python packs/search/primitives/contracts/contracts.py check-postgres --env-file .env
uv run --project . python packs/search/primitives/contracts/contracts.py dump-postgres --env-file .env --out .powerpacks/schema-dumps/postgres-live.json
```

`dump-postgres` writes a diagnostic artifact. It does not mutate the checked-in
contracts.

## Runtime Keys

Provisioned users can populate a local `.env` without pasting raw secrets into
chat:

```bash
uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull \
  --env-file .env
```

The primitive redacts secret values in output and only writes the local runtime
keys returned by the authenticated Powerset API. Modal holds hosted processing
secrets for provisioned Powerset users. The Google Cloud CLI is still used by
the separate msgvault/Gmail OAuth app setup flow, not by Powerset runtime-key
pull.

## Architecture

Start at [`docs/README.md`](docs/README.md). The canonical product guides are:

- [`$search` architecture](packs/search/docs/search-architecture.md)
- [LinkedIn and Modal indexing pipeline](packs/indexing/docs/linkedin-modal-pipeline.md)

Retired task-flow, slice-planning, and design-plan bodies live in Git history
rather than the current documentation tree.

## Development

```bash
scripts/lint-powerpacks
scripts/test-powerpacks
```

The lint command runs `ruff` and `flake8` through `uv` using the repo lockfile.

## Testing

Use `scripts/test-search check` for local readiness. For parallel query
expansion and recall harness details, see `docs/testing.md`.

## Current Scope

V1 is intentionally narrow:

- query decomposition from natural language, job descriptions, or URLs
- role-first people search with optional company constraints
- recall-style filters: education, tenure, years of experience, age, seniority
- company-side filters: headcount, funding, valuation, founded year, sector,
  entity type, company geography
- company-domain adjacency only when requested, confirmed, or explicitly
  planned as an exploratory slice
- TurboPuffer as the primary search surface
- Postgres as the hydration/supporting data surface
- conservative LLM filtering after full-frontier hydration
- CSV/JSONL/manifest artifact persistence for refinement

Excluded from the public V1 surface:

- internal/private join logic
- Sales Nav
- repo-specific internal connector details
- broad enrichment workflows
- company summary or company-signal search
- expensive scoring/reranking

## Adapter Notes

NanoClaw-specific pieces now live under `adapters/nanoclaw/`:

- `install.sh` installs Powerpacks into a NanoClaw checkout
- `runtime/` contains the threaded CLI channel and container patches
- `bin/powerclaw` is the legacy NanoClaw terminal wrapper
- `primitives/view_search_results/` is the legacy NanoClaw TUI

Those pieces are not part of the portable primitive surface.


## Powerset-hosted config

Hosted Powerset commands require explicit API/MCP/Auth0 config. Copy `packs/powerset/templates/env.powerset.example` to `.env` for Powerset-hosted use.
