# Powerpacks Quickstart

Set up Powerpacks on a new machine, install it into your agent host, and run
your first command in each skill family.

This walkthrough assumes:

- macOS or Linux laptop
- you can install software with Homebrew (macOS) or apt (Linux)
- you have a Powerset account (for `$powerset login`)

If you only want a subset of skills, you can skip the prereq sections you
don't need.

---

## 1. Prereqs

### Common (every skill)

```bash
# uv + git. Powerpacks uses uv-managed Python 3.12 from .python-version.
uv --version
git --version
```

macOS users on a fresh box:

```bash
xcode-select --install
brew install uv git
```

The adapter install also runs `bin/setup-python`; if `uv` is missing and
Homebrew is available, setup installs `uv` automatically.

### `search-network` / `search-company`

These hit Powerset infrastructure, so you need a working `.env`. Run
`$powerset login` (below) to populate it, or copy `packs/powerset/templates/env.example` to
`.env` and fill it in manually.

### `$powerset login` (recommended setup path)

Powerset employees only. Pulls allowlisted secrets from GCP Secret Manager
into a local `.env`.

```bash
brew install --cask google-cloud-sdk    # macOS
# or: curl https://sdk.cloud.google.com | bash

gcloud auth login                       # use your @powerset.co account
gcloud config set project powerset-prod
```

The skill refuses to provision unless your active gcloud account ends in
`@powerset.co`.

### `import-imessage`

macOS only. Reads `~/Library/Messages/chat.db` and the AddressBook databases
in **read-only mode**.

You must grant **Full Disk Access** to whatever terminal / IDE you'll launch
the skill from:

1. Open `System Settings → Privacy & Security → Full Disk Access`
2. Add your terminal app (`Terminal`, `iTerm`, `WezTerm`, `VS Code`,
   `Cursor`, etc.) — the same one Codex / Claude Code is launched from
3. Restart the terminal app

The primitive is stdlib-only; no new Python packages required.

### `import-whatsapp`

Needs Docker so we can run a local [WAHA](https://github.com/devlikeapro/waha)
container. Two options:

```bash
# Option A: Docker Desktop (GUI, requires accepting EULA on macOS)
brew install --cask docker
open -a Docker

# Option B: Colima (lightweight, no EULA, recommended for Apple Silicon)
brew install colima docker
colima start --memory 2 --vm-type vz --vz-rosetta
```

You'll also need WhatsApp on your phone to scan the QR code that pops up
during the auth step.

### `import-contacts-review`

The login step uses a browser-based Auth0 flow on `127.0.0.1:9876`. The LLM
review step uses [OpenRouter](https://openrouter.ai/):

```bash
# After signup, store your key wherever your shell reads env from:
export OPENROUTER_API_KEY=sk-or-...
```

Or pass `--api-key` explicitly when running the LLM review.

---

## 2. Install

For Codex, start with the agent-native path:

```bash
codex exec "Clone or update https://github.com/powerset-co/powerpacks in the current directory, then run the Codex install step from its instructions."
```

For other hosts, or if you want to run the installer manually, pick the adapter
from a local checkout:

```bash
git clone https://github.com/powerset-co/powerpacks.git
cd powerpacks

./install.sh codex                       # → ~/.codex/skills/
./install.sh claude-code                 # → ~/.claude/skills/
./install.sh pi                          # → ~/.pi/agent/skills/
./install.sh nanoclaw /path/to/nanoclaw
```

Each adapter copies the Powerpacks skills as `<skill>/SKILL.md` plus a sibling
`<skill>/powerpacks/` bundle that holds the primitives, schemas, contracts,
tasks, and packs the skill resolves at runtime.

**Restart your agent host** after install so it reloads the skill list. In Pi,
you can also run `/reload`.

---

## 3. Sanity-check your install

```bash
# from the repo root
scripts/test-powerpacks    # runs the unit-test suite
scripts/lint-powerpacks    # runs ruff + flake8
```

The smoke script drives every messages-pack primitive end-to-end on synthetic
data (no network, no spend, no QR scan):

```bash
scripts/smoke-messages.sh
```

---

## 4. First run, per skill

### `$powerset login` — bootstrap your `.env`

Inside Codex / Claude Code / Pi:

```text
$powerset login
```

It runs the Powerset doctor, starts Auth0 login if needed, pulls allowlisted
secrets into `.env` (no values printed), and installs/refreshes the
`powerset-search` MCP. It does not check gcloud application-default credentials;
ADC is not needed for normal Powerpacks workflows.

Pick a profile based on what you'll use:

| Profile | Includes |
| --- | --- |
| `search-core` | TurboPuffer + Postgres + OpenAI |
| `messages` | OpenRouter + Parallel |
| `sales-nav` | RapidAPI |
| `supabase-admin` | Supabase URL + service role |
| `all` | every allowlisted key |

### `$search-network` — recruiting search

```text
$search-network senior infra engineers at fintech infra startups in NYC, Stanford
```

The skill walks you through decomposition → plan → user approval → retrieval →
hydration → CSV/JSONL artifact. Outputs land under `.powerpacks/runs/...`.

See `packs/search/docs/task-flow.md` for the full lifecycle and the
`extract-search-query` sub-skill boundary.

### `$search-company` — company resolution

```text
$search-company crypto trading infra companies that raised series B
```

Resolves to canonical TurboPuffer company IDs you can hand to
`search-network` as `company_filter`.

### Messages pack — `$import-contacts`

Use the one-command guided harness for the normal path:

```text
$import-contacts              # iMessage + WhatsApp → merge → match → review
```

Advanced/debug subflows remain available:

```text
$import-imessage              # extracts ~/Library/Messages/chat.db → imessage.contacts.csv
$import-whatsapp              # boots WAHA, you scan a QR, → whatsapp.contacts.csv
                             # both also auto-merge into contacts.csv
$import-contacts-review       # login → sync candidates → match → LLM ENRICH/SKIP
```

Artifacts land under `.powerpacks/messages/`:

```text
.powerpacks/messages/
├── imessage.contacts.csv         per-channel exports
├── whatsapp.contacts.csv
├── contacts.csv                  unified, dedup'd by phone
├── powerset_contacts.csv         your candidate catalog (after sync)
├── *.manifest.json               per-step counts + diagnostics
└── whatsapp/qr.png               most-recent QR for WAHA auth
```

The pack is privacy-first:

- never reads or stores message bodies
- LLM review only sends `name`, `source`, `message_count`, recency,
  `is_in_group_chats`, and `group_names` — no phones, no content
- every step requires explicit user approval

---

## 5. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `extract_imessage_contacts check` reports `chat_db.readable: false` | Grant Full Disk Access to the terminal app and restart it. |
| `waha_runtime check` says `docker.daemon_ok: false` | Start Docker Desktop (`open -a Docker`) or Colima (`colima start`). |
| `provision_runtime_env pull` says "no active gcloud account" | `gcloud auth login` with your `@powerset.co` account. |
| `provision_runtime_env pull` rejects your account | Switch active accounts: `gcloud config set account you@powerset.co`. |
| `auth login` browser callback never returns | Make sure nothing else is listening on `127.0.0.1:9876`. |
| `llm_review_contacts review` says "OPENROUTER_API_KEY not provided" | `export OPENROUTER_API_KEY=sk-or-...` or pass `--api-key`. |
| Codex / Claude Code / Pi doesn't see the new skills | Restart the host. In Pi, `/reload` also reloads skills. |

For deeper diagnostics, every primitive writes a JSON manifest with counts,
diagnostics, and timings. Look under `.powerpacks/runs/` and
`.powerpacks/messages/` for the latest artifacts.
