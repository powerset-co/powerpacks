# Powerpacks

`powerpacks` is a portable bundle of skills + deterministic primitives +
check-in data contracts that turn a coding-agent host (Codex, Claude Code,
NanoClaw) into a recruiting-search and contact-import workstation backed by
Powerset.

The core package is host-agnostic. The same skills run unchanged across hosts;
only the install adapter differs.

## Skills

User-facing skill entrypoints, grouped by purpose. Each skill ships its own
`SKILL.md` with the full workflow.

### Search

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`search-network`](packs/search/skills/search-network/SKILL.md) | `/search-network <query>` | Role-first people search. Decomposes a NL query / job description / URL, plans, retrieves from TurboPuffer, hydrates from Postgres, optionally reranks, persists CSV/JSONL artifacts. |
| [`search-company`](packs/search/skills/search-company/SKILL.md) | `/search-company <query>` | Resolves company names, descriptions, sectors, investor/funding filters into canonical TurboPuffer company IDs. |
| [`extract-search-query`](packs/search/skills/extract-search-query/SKILL.md) | called by `search-network` | Sub-skill for headless query decomposition. |

### Setup

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`powerset-login`](packs/powerset/skills/powerset-login/SKILL.md) | `$powerset-login` | Single Powerset login flow: Auth0 PKCE for your user JWT, then best-effort `.env` provisioning from GCP Secret Manager. Privileged users get TurboPuffer / Postgres / OpenAI / OpenRouter / Parallel / RapidAPI keys; everyone else still ends up with a usable Auth0 token. Also installs the `powerset-search` MCP into Claude Code / Codex. |

### Sales Nav

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`sales-nav-search`](packs/sales-nav/skills/sales-nav-search/SKILL.md) | `$sales-nav-search` | Run a Sales Navigator search through the `powerset-search` MCP. Resolves company / title filters, runs a paginated lead search with server-side artifact persistence on by default, paginates via `get_artifact`. Depends on `$powerset-login` having run first. |

### Messages pack

| Skill | Trigger | What it does |
| --- | --- | --- |
| [`import-imessage`](packs/messages/skills/import-imessage/SKILL.md) | manual | Read local macOS Messages SQLite, extract phone+name+volume metadata only. No bodies. |
| [`import-whatsapp`](packs/messages/skills/import-whatsapp/SKILL.md) | manual | Run a local [WAHA](https://github.com/devlikeapro/waha) Docker container, scan a QR with your phone, extract WhatsApp contact metadata. No bodies. |
| [`import-contacts-review`](packs/messages/skills/import-contacts-review/SKILL.md) | manual | After import: log in to Powerset, sync your operator catalog, run local name matching, LLM-review the unmatched contacts (ENRICH/SKIP). |

## Goal

- make TurboPuffer and Postgres contracts explicit enough that agents do not
  guess field names, operators, or value types
- give the agent operational entrypoints: `/search-network <query>`,
  `/search-company <query>`, `$powerset-login`, and the messages-pack import
  skills
- decompose broad recruiting queries into bounded retrieval plans
- persist task state and CSV/JSONL artifacts so users can refine prior runs
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
│   │   ├── skills/         powerset-login (one unified login flow)
│   │   ├── primitives/     auth/ (Auth0 PKCE),
│   │   │                   provision_runtime_env/ (best-effort GCP pull),
│   │   │                   provision_user_secrets/ (admin: per-user GCP),
│   │   │                   doctor/ (one-shot setup health check),
│   │   │                   mcp_install/ (powerset-search MCP into
│   │   │                                 Claude Code / Codex)
│   │   └── templates/      env.example
│   ├── sales-nav/          Sales Navigator search via the powerset-search MCP
│   │   └── skills/         sales-nav-search
│   ├── search/             recruiting people / company search
│   │   ├── skills/         search-network, search-company,
│   │   │                   extract-search-query
│   │   ├── primitives/     ~21 search primitives + lib/ + contracts CLI +
│   │   │                   task_state/
│   │   ├── schemas/        decomposed-query, role-search-filters,
│   │   │                   task-run.schema.json, etc.
│   │   ├── contracts/      checked-in Postgres + TurboPuffer schemas
│   │   ├── tasks/          search-network.task.json
│   │   ├── docs/           search-surface, slice-planning, turbopuffer-*,
│   │   │                   harnesses/, workflows/
│   │   └── evals/          recall, company-search, founder parity
│   └── messages/           iMessage + WhatsApp + Powerset enrichment
│       ├── skills/         import-imessage, import-whatsapp,
│       │                   import-contacts-review
│       ├── primitives/     iMessage / WAHA / matching / LLM-review /
│       │                   deep-research primitives
│       ├── schemas/        message-contact, messages-run-manifest
│       ├── tasks/          import-*.task.json
│       └── docs/           harness.md
├── adapters/               codex/, claude-code/, nanoclaw/ installers
├── docs/                   cross-pack docs (quickstart.md, testing.md)
├── scripts/                test-powerpacks, lint-powerpacks, smoke-messages.sh
├── tests/                  cross-pack test suite
└── templates/              host-install templates (claude-fragments,
                            container.json)
```

The `powerset` pack is the foundation — every other pack depends on its
`auth` and `task_state` primitives. Anyone using Powerpacks runs
`$powerset-login` first.

## Quickstart for a fresh account

A full first-run walkthrough — prereqs, install, and your first command per
skill — is in [`docs/quickstart.md`](docs/quickstart.md). The short version:

```bash
# 1. clone
git clone https://github.com/<org>/powerpacks.git && cd powerpacks

# 2. install skills into your agent host (pick one)
./install.sh codex                # → ~/.codex/skills/
./install.sh claude-code          # → ~/.claude/skills/
./install.sh nanoclaw /path/to/nanoclaw

# 3. restart the agent host so it reloads the skill list

# 4. inside the agent, run any of:
/search-network senior infra eng at fintech
/search-company stripe-like fintech infra companies
$powerset-login                   # provisions .env from GCP Secret Manager
# or trigger the messages pack manually:
#   "import my iMessage contacts" → import-imessage
#   "import my whatsapp contacts" → import-whatsapp
#   "review my imported contacts" → import-contacts-review
```

### Prereqs by skill family

| You want to use… | Install on the host running Codex / Claude Code |
| --- | --- |
| Any skill | Python 3.9+ (`python3 --version`), git |
| `search-network` / `search-company` | `.env` with `TURBOPUFFER_API_KEY`, `DATABASE_URL`, `OPENAI_API_KEY` (use `$powerset-login` to populate) |
| `powerset-login` | `gcloud` CLI, `@powerset.co` Google account: `brew install --cask google-cloud-sdk && gcloud auth login` |
| `import-imessage` | macOS, **Full Disk Access** for your terminal (`System Settings > Privacy & Security > Full Disk Access`) so Python can read `~/Library/Messages/chat.db` |
| `import-whatsapp` | Docker (`brew install --cask docker` or `brew install colima docker`), the WhatsApp app on your phone for QR scan |
| `import-contacts-review` | Auth0 login via browser (popped automatically), `OPENROUTER_API_KEY` for the LLM-review step |
| `sales-nav-search` | `$powerset-login` already run (it ships the Auth0 token + registers the `powerset-search` MCP into your host) |

## Install

The top-level `install.sh` dispatches to a per-host adapter. **All three
adapters are idempotent — re-run them any time skills change** (you do not need
to uninstall first; each adapter wipes and re-copies the skill directories).

### Codex

```bash
./install.sh codex                          # default: ~/.codex/skills/
./install.sh codex /custom/skills/dir       # explicit target
```

### Claude Code

```bash
./install.sh claude-code                    # default: ~/.claude/skills/
./install.sh claude-code ./.claude/skills   # project-level install
```

### NanoClaw

```bash
./install.sh nanoclaw /path/to/nanoclaw
```

Restart the agent host after install so it reloads the skill list. Direct
adapter installs also work:

```bash
./adapters/codex/install.sh                    [skills-dir]
./adapters/claude-code/install.sh               [skills-dir]
./adapters/nanoclaw/install.sh /path/to/nanoclaw
```

The NanoClaw adapter copies the core Powerpacks directory into the target,
installs `search-network` and its `extract-search-query` sub-skill, wires the
threaded CLI channel, and keeps NanoClaw-specific TUI/runtime code under
`powerpacks/adapters/nanoclaw`.

### Reinstall after pulling new changes

```bash
cd ~/workspace/powerpacks
git pull
./install.sh codex          # or claude-code, or nanoclaw <path>
# then restart Codex / Claude Code so it re-reads the skill list
```

This is the only command needed for skill / primitive changes. The `mcp_install`
registrations are written to host config files (`~/.codex/config.toml`,
`~/.claude.json`) and only need re-running when the MCP URL or token format
changes — `$powerset-login` covers that path.

### MCP install (powerset-search)

The `sales-nav-search` skill and any future MCP-driven skills need the remote
`powerset-search` MCP registered with your agent host. `$powerset-login`
does this for you as part of its setup flow, but you can also run it directly:

```bash
python3 packs/powerset/primitives/mcp_install/mcp_install.py install --host all
# verify
claude mcp list                  # for Claude Code
codex mcp list 2>/dev/null \
  || python3 packs/powerset/primitives/mcp_install/mcp_install.py status --host codex
```

Claude Code bakes the bearer token into `~/.claude.json` at install time.
Codex reads it from `POWERPACKS_POWERSET_TOKEN` at runtime, so after
`$powerset-login` you also need:

```bash
eval "$(python3 packs/powerset/primitives/mcp_install/mcp_install.py token-env)"
```

## Verify your install

Quick checks that each layer works — run from the repo root after
`./install.sh <host>`:

```bash
# 1. Skill files actually copied to the host
ls ~/.codex/skills/                # or ~/.claude/skills/

# 2. Powerpacks unit tests
python3 -m unittest discover -s tests

# 3. Messages-pack end-to-end smoke (synthetic data, no network/spend)
scripts/smoke-messages.sh

# 4. MCP reachability (after $powerset-login)
claude mcp list                    # "powerset-search ... ✓ Connected"
python3 packs/powerset/primitives/doctor/doctor.py run
```

Then, **inside the agent host**, sanity-check each skill family:

| Skill | Test prompt |
| --- | --- |
| `powerset-login` | Type `$powerset-login` — the agent should run `gcloud auth list`, show the secret plan, ask for approval, and finish with `mcp_install`. |
| `search-network` | `/search-network senior infra engineers in NYC` — should produce a plan + approval prompt, not retrieve anything yet. |
| `sales-nav-search` | `$sales-nav-search VPs of engineering at Stripe` — should resolve company id, run the search, return a first page of leads + an `artifact_id`. |
| `import-imessage` | "import my iMessage contacts" — the agent should detect Full Disk Access status before reading. |

If the agent host doesn't see a skill at all: re-run `./install.sh <host>`
and restart the host (skills are loaded once at startup).



## Contracts

Powerpacks treats Postgres and TurboPuffer schema as checked-in contracts, not
something the agent should rediscover on each run:

```bash
python powerpacks/packs/search/primitives/contracts/contracts.py list
python powerpacks/packs/search/primitives/contracts/contracts.py check-postgres --env-file .env
python powerpacks/packs/search/primitives/contracts/contracts.py dump-postgres --env-file .env --out .powerpacks/schema-dumps/postgres-live.json
```

`dump-postgres` writes a diagnostic artifact. It does not mutate the checked-in
contracts.

## Runtime Provisioning

Internal Powerset users can provision a local `.env` from GCP Secret Manager
without pasting raw secrets into chat:

```bash
gcloud auth login
python powerpacks/packs/powerset/primitives/provision_runtime_env/provision_runtime_env.py pull \
  --profile search-core \
  --env-file .env \
  --confirm
```

The provisioning primitive redacts secret values in output and only writes
allowlisted keys. Authorization is enforced by GCP IAM on Secret Manager
resources. For user-scoped keys, create per-user/per-capability secrets and
grant access on those specific secret resources or groups.

## Task Flow

See `packs/search/docs/task-flow.md` for the current search task lifecycle,
the extraction sub-skill boundary, and the difference between primitive parity
harnesses and agent extraction harnesses.

## Development

```bash
scripts/lint-powerpacks
scripts/test-powerpacks
```

The lint command runs `ruff` and `flake8` through `uv` using the repo lockfile.

## Testing

Use `scripts/test-search-network check` for local readiness. For headless Codex
extraction and recall harness details, see `docs/testing.md`.

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
