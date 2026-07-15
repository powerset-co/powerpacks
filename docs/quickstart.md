<!--
Changelog:
- 2026-07-16: $import-messages and $import-gmail are contact-sync only.
  Removed the OpenRouter prerequisite and the llm_review_contacts
  troubleshooting row, replaced research_queue.csv/research_review.csv with
  import/messages/candidates.csv in the artifact tree, and noted that identity
  research and index builds now happen in $deep-setup.
-->

# Powerpacks Quickstart

Set up Powerpacks on a new machine, install it into your agent host, and run
your first command in each skill family.

This walkthrough assumes:

- macOS or Linux laptop
- you can install software with Homebrew (macOS) or apt (Linux)
- you have a Powerset account (for `$powerset setup`)

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

### `search` / `search-company`

Use `$search <jd-or-brief>` when you want the agent to do the work. For
complex JDs, it plans the recruiter loop internally, shows one search-plan approval, then
orchestrates the planned searches against Powerset infrastructure. The default
LLM review budget is 100 unique profiles across initial probes plus fan-out; this
limits expensive review/rerank volume, not retrieval/count checks or final found
count.

`$search` and `$search-company` hit Powerset infrastructure, so you need
a working `.env`. Run `$powerset setup` (below) to populate it, or copy
`packs/powerset/templates/env.example` to `.env` and fill it in manually.

### `$powerset setup` (recommended setup path)

For provisioned users, pulls local runtime keys from the authenticated Powerset
API into `.env`, ensures Auth0 login, and installs/refreshes the
`powerset-search` MCP. Modal handles hosted processing for Powerset users.

The Google Cloud CLI is only needed for the separate msgvault/Gmail OAuth app
setup flow.

### Messages import prerequisites

macOS only. Reads `~/Library/Messages/chat.db` and the AddressBook databases
in **read-only mode**.

You must grant **Full Disk Access** to whatever terminal / IDE you'll launch
the skill from:

1. Open `System Settings → Privacy & Security → Full Disk Access`
2. Add your terminal app (`Terminal`, `iTerm`, `WezTerm`, `VS Code`,
   `Cursor`, etc.) — the same one Codex / Claude Code is launched from
3. Restart the terminal app

The primitive is stdlib-only; no new Python packages required.

WhatsApp uses the local
[`wacli`](https://github.com/openclaw/wacli) helper by default. Canonical
discovery does not install software silently: if wacli or its QR renderer is
missing, `$import-messages` shows the exact Homebrew command and asks before
running it. The wacli command is:

```bash
brew install steipete/tap/wacli
```

QR rendering may separately request `brew install qrencode`.

You'll also need WhatsApp on your phone to scan the QR code that pops up
during the auth step.

No LLM key is needed: message import makes no OpenRouter or other provider
calls (identity research happens later, in `$deep-setup`).

---

## 2. Install

For Codex, start with the agent-native path:

```bash
codex exec "Clone https://github.com/powerset-co/powerpacks if needed, then cd into powerpacks and run bin/update-codex."
```

For Claude Code:

```bash
claude -p "Clone https://github.com/powerset-co/powerpacks if needed, then cd into powerpacks and run bin/update-claude-code."
```

For other hosts, or if you want to run the installer manually, pick the adapter
from a local checkout:

```bash
git clone https://github.com/powerset-co/powerpacks.git
cd powerpacks

bin/update-codex                         # Codex: pull, sync agent files, reinstall skills/profile
bin/update-claude-code                   # Claude Code: pull, sync agent files, reinstall skills
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

---

## 4. First run, per skill

### `$powerset setup` — login, `.env`, and MCP

Inside Codex / Claude Code / Pi:

```text
$powerset setup
```

It runs the Powerset doctor, starts Auth0 login if needed, pulls provisioned
runtime keys into `.env` (no values printed), and installs/refreshes the
`powerset-search` MCP. `$powerset login` remains available as a smaller
credential-refresh/backcompat command.

### `$search` — recruiting search

```text
$search senior infra engineers at fintech infra startups in NYC, Stanford
```

The skill first records the requested result surface, backend, and depth.
Standard search prepares one query preview, confirms it once, then retrieves,
hydrates, filters, and ranks. Deep search builds and critiques a recruiter
contract, stops once for Review, then runs bounded candidate-archetype probes,
evidence judging, deterministic gates, and anchor expansion autonomously.
Artifacts land under `.powerpacks/search/...` or
`.powerpacks/deep-search/...`.

See the [`$search` architecture](../packs/search/docs/search-architecture.md)
for the full lifecycle and the
[deep-mode runbook](../packs/search/skills/search/deep-mode.md) for exact deep
commands and artifacts.

### `$search-company` — company resolution

```text
$search-company crypto trading infra companies that raised series B
```

Resolves to canonical TurboPuffer company IDs you can hand to
`search` as `company_filter`.

### Message ingestion - `$import-messages`

Use the one-command guided harness for the normal path:

```text
$import-messages              # iMessage + WhatsApp -> match -> import matched + candidates -> merge
```

Use the underlying primitives directly for advanced/debug subflows.

Intermediate extraction and provider artifacts land under
`.powerpacks/messages/`:

```text
.powerpacks/messages/
├── imessage.contacts.csv         per-channel exports
├── whatsapp.contacts.csv
├── contacts.csv                  unified, dedup'd by phone
├── *.manifest.json               leaf-primitive counts + diagnostics
└── wacli-login-qr.html           browser QR page for WhatsApp auth
```

The fixed source-stage contracts live alongside those intermediates:

```text
.powerpacks/network-import/discover/messages/{contacts.csv,manifest.json}
.powerpacks/network-import/import/messages/{people.csv,candidates.csv,manifest.json}
.powerpacks/network-import/merged/people.csv
```

The workflow's message-content boundary is strict:

- Powerpacks never selects or sends message bodies; wacli owns its local provider
  store.
- Import makes no provider calls at all — no OpenRouter, no Parallel, no
  RapidAPI, no Modal. Matched contacts import directly; unmatched contacts
  that pass a deterministic floor land in `import/messages/candidates.csv`, a
  research pool for `$deep-setup` (identity research, review, and the index
  rebuild happen there).
- Nothing is uploaded to a Powerset set.

See the [iMessage and WhatsApp import pipeline](../packs/ingestion/docs/message-import-pipeline.md)
for the complete diagram, floor rules, and approval gates.

### Gmail — `$import-gmail`

`$import-gmail` links selected Gmail accounts through msgvault, performs one
bounded multi-account discovery, reuses the local identity directory, and
merges sources — free and local, with no LinkedIn lookups at import time and
no index rebuild. Unresolved contacts land in `import/gmail/candidates.csv`
for `$deep-setup`, which resolves identities and rebuilds the index. msgvault
keeps a local full-message archive for that window and may store attachments;
Powerpacks selects only contact/interaction metadata and does not send Gmail
bodies or subjects to identity providers. See the
[Gmail import pipeline](../packs/ingestion/docs/gmail-import-pipeline.md).

### Process your contacts — `$deep-setup`

After any import finishes it asks *"process your contacts now?"* — a yes runs
`$deep-setup`, the centralized processing layer (it never runs silently; every
paid stage previews its cost and the review step is a hard stop):

```text
$deep-setup                   # or: "process my contacts"
```

It builds one dossier per contact from message bodies — including the imports'
research candidates — and the synthesis LLM judges each contact's
**network worth (yes / maybe / no)** from the actual relationship. You review
in the browser UI: mark Yes/Maybe/No per person (your mark overrules the LLM
and sticks), filter by worth and by source (gmail / imessage / whatsapp), and
keep/detach/fix LinkedIn attachments. Contacts marked **No** cost nothing —
they're excluded from the paid reverse lookup. Then one Parallel.ai reverse
lookup resolves the survivors, no-LinkedIn people get synthetic profiles, and
the finale re-merges and rebuilds the Modal index so everything becomes
searchable. See the
[deep-setup pipeline](../packs/ingestion/docs/deep-setup-pipeline.md).

### Relationship dossiers — `$deep-context`

`$deep-context` is the ad-hoc surface over the same dossiers: person lookups
("who is <name/phone>?"), re-reviews, and the review UI. It reads Gmail and
chat bodies (the explicit exception to metadata-only import); small iMessage
group bodies require an explicit current-run opt-in. See the
[deep-context pipeline](../packs/ingestion/docs/deep-context-pipeline.md).

---

## 5. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `extract_imessage_contacts check` reports `chat_db.readable: false` | Grant Full Disk Access to the terminal app and restart it. |
| `import_whatsapp_wacli run` cannot install `wacli` | Approve the Homebrew install when prompted; if `qrencode` is missing, install the exact dependency the primitive reports. |
| `$powerset env pull` reports `not_provisioned` | Ask a Powerset admin to provision your Modal/OpenAI runtime keys, then rerun `$powerset setup`. |
| `auth login` browser callback never returns | Make sure nothing else is listening on `127.0.0.1:9876`. |
| Codex / Claude Code / Pi doesn't see the new skills | Restart the host. In Pi, `/reload` also reloads skills. |

For deeper diagnostics, inspect the fixed stage manifests under
`.powerpacks/network-import/`, the leaf manifests under
`.powerpacks/messages/`, and indexing progress under `.powerpacks/runs/`.
