---
name: setup
description: Deterministic Powerpacks setup. Use for $setup. Steps through update, Powerset login, runtime-key pull, systematic Gmail/msgvault setup, Gmail sync/import, LinkedIn import, source merge, Modal index, and search-index validation. Always reruns the full checklist; overwrites in place.
---

<!--
Created: 2026-06-17
Changelog:
- 2026-06-17: Promoted the former $setup-beta to $setup. Setup now does the full
  multi-source flow: systematic msgvault/Gmail setup + the unified merge+index
  path (fan-in -> Modal index-people), with LinkedIn enriched on Modal
  (import-linkedin). Replaces the prior LinkedIn-only fast path.
-->

# setup

`$setup` connects your sources and builds one local search index: same prereqs,
then **systematic Gmail/msgvault setup**, then a **unified merge + index** that
combines LinkedIn + Gmail into one local search index.

It runs a **fixed checklist and always reruns it end to end**. Reruns
are idempotent against fixed paths. The one exception is the msgvault store
(`~/.msgvault/msgvault.db`): it is the durable, incrementally-synced archive —
**never delete it**; re-syncs resume from the last message via `--after`.

## How to run this skill

**FIRST, before running anything: create a literal, visible checklist with all
fourteen steps below and step through it, marking each item complete as you go.**
Mandatory. Use your harness's plan/todo/task tool:

- **Claude Code:** `TaskCreate` one task per step (0–13), then `TaskUpdate` each
  to `in_progress` then `completed`.
- **Codex:** `update_plan` with the fourteen steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

Seed the checklist with these exact item titles:

```
0.  Update Powerpacks (bin/update-codex)
1.  Check Powerset login + credentials
2.  Log in to Powerset (only if not logged in)
3.  Pull runtime keys (Modal + OpenAI)
4.  Check msgvault status
5.  Ask which Gmail accounts to link
6.  Create msgvault OAuth app (browser, if not configured)
7.  Authorize Gmail accounts
8.  Sync Gmail archives (msgvault)
9.  Import Gmail contacts (resolve -> people.csv)
10. Import LinkedIn Connections.csv
11. Merge all sources
12. Index the merged network
13. Validate the search index
```

Some steps are conditional (2 if already logged in, 6 if OAuth is already
configured). Keep them in the checklist and mark them complete as a no-op when
they don't apply — do not drop them.

Then:

1. **Work the checklist in order 0 → 13.** Exactly one item `in_progress` at a
   time; mark it `completed` before the next. No batching, reordering, skipping.
   One-line result per step.
2. **Run from the canonical repo root.** Resolve once and `cd` there (see *Repo
   root*). `.powerpacks/...` paths are relative to that root.
3. **Deterministic & in-place.** Overwrite the fixed derived paths
   (`.powerpacks/network-import/...`, `.powerpacks/search-index/...`); rely on
   the primitives to overwrite. Don't pre-delete with `rm` and don't invent
   timestamped/alternate folders. **Never delete `~/.msgvault/msgvault.db`** —
   it is the durable Gmail archive and re-syncs incrementally.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** This skill is self-contained and
  authoritative. Do not read agent memory, prior state, other docs, or primitive
  source to re-derive paths. Build the checklist and execute it directly.
- **Do not edit code.** Only if you hit an actual blocking bug, and say so.
  Otherwise only invoke the primitives below.
- **Do not write scripts to do the work.** Reuse the exact primitive commands.
  Plain shell for `cp`/`test`/`wc`/`cat` is fine.
- **Consent gates (pause for the user):** Powerset browser login; msgvault
  browser/gcloud OAuth-app creation and Gmail account authorization; and Gmail
  Parallel.ai spend at/above the auto-approve threshold (Step 5). Everything
  else runs without asking.

### Repo root

```bash
resolve_powerpacks_root() {
  for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" != *"/.codex/"* ]] || continue
    if [[ -x "$candidate/bin/update-codex" && -d "$candidate/packs" ]]; then
      printf '%s\n' "$candidate"; return 0
    fi
  done
  return 1
}
REPO="$(resolve_powerpacks_root)" || { echo "Install Powerpacks to ~/powerpacks first." >&2; exit 1; }
cd "$REPO"
```

---

## The checklist

### Step 0 — Update Powerpacks

```bash
cd "$REPO" && bin/update-codex
```

### Step 1 — Check Powerset login + credentials

```bash
test -f "$HOME/.powerpacks/credentials.json" && echo "credentials.json: present" || echo "credentials.json: MISSING"
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py whoami
```

If missing or `whoami` fails → Step 2. Otherwise Step 2 is a no-op → Step 3.

### Step 2 — Log in (only if Step 1 said not logged in)

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py login
```

Browser consent. If it can't open, print the URL for the user. Re-run `whoami`.

### Step 3 — Pull provisioned runtime keys (Modal + OpenAI)

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull --env-file .env
```

Verify: `… pull_runtime_keys.py check --env-file .env`.

### Step 4 — Check msgvault status

Safe, local. Drives the next two steps:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py status
```

The JSON reports `gcloud`, `config.oauth_configured`, `database.exists`, and
authorized `accounts`. If `oauth_configured` is true and the db exists, Step 6 is
a no-op.

### Step 5 — Ask which Gmail accounts to link

Ask the user for **every** Gmail address they want searchable, in one prompt.
Treat the first as the **primary** (used to create the OAuth app in Step 6); the
rest are additional accounts authorized in Step 7. Compare against the already
`accounts` from Step 4 — only accounts not already authorized need Steps 6–7.
Record the list and carry it into the next two steps. Do not guess emails.

### Step 6 — Create msgvault OAuth app (browser, if not configured)

Only if Step 4 showed OAuth not configured. One-time browser setup for the
**primary** Gmail account from Step 5 — **consent: drives Chrome + gcloud,
creates the Google OAuth Desktop app, inits the db, authorizes the primary
account**:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup \
  --email <primary-gmail> --add-account --init-db
```

Writes `~/.msgvault/config.toml`, the client secret, and `~/.msgvault/msgvault.db`
(do not delete the db). If already configured, mark this step a no-op.

### Step 7 — Authorize Gmail accounts

For each **additional** account from Step 5 (the primary is already authorized) —
**consent: per-account browser OAuth grant**:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-account --email <email>
```

Re-run `msgvault_setup.py status` and confirm every requested account appears
under `accounts`.

### Step 8 — Sync Gmail archives (msgvault)

For each authorized account, sync the archive and build the discover artifacts.
**Default the sync window to the last 3 years** via `--sync-after` so the first
sync is bounded (not the full mailbox history):

```bash
cd "$REPO"
SYNC_AFTER="$(date -v-3y +%Y-%m-%d 2>/dev/null || date -d '3 years ago' +%Y-%m-%d)"
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/gmail.py discover \
  --account-email <email> --sync-after "$SYNC_AFTER"
```

(Repeat per account, or omit `--account-email` for all linked accounts.) Writes
`.powerpacks/network-import/discover/gmail/<account>/`. The 3-year window is the
default; if the user asks for more/less history, change `-v-3y` (e.g. `-v-5y`)
or pass a specific `--sync-after YYYY-MM-DD`. A large first sync can take a while;
msgvault skips already-downloaded messages on reruns.

### Step 9 — Import Gmail contacts (resolve → people.csv)

Import resolves contacts to LinkedIn via **Parallel.ai**, then writes
`.powerpacks/network-import/import/gmail/people.csv`. Run **without** the spend
flag first — the primitive auto-approves small batches (under its threshold) and
otherwise blocks:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run
```

- If it completes (small batch auto-approved), proceed.
- **If it blocks reporting pending Parallel contacts** (at/above the threshold),
  tell the user the contact count and ask to approve the spend. On approval:

  ```bash
  cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run --approve-parallel-spend
  ```

### Step 10 — Import LinkedIn Connections.csv

Ask the user for their `Connections.csv` path. Place it at the canonical input
(overwrite), then enrich it **on Modal** — the same shared enrichment + cache
prod uses — and download the enriched people.csv to the path the merge reads:

```bash
cd "$REPO"
DEST=".powerpacks/network-import/discover/linkedin/Connections.csv"
mkdir -p "$(dirname "$DEST")"
cp -f "<user-csv-path>" "$DEST"
uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py import-linkedin --csv "$DEST"
```

This runs only the Modal import/enrich stage (no local DuckDB) and writes the
enriched `.powerpacks/network-import/import/linkedin/people.csv` for the merge.
Because enrichment runs on Modal it needs no local RapidAPI key, and the shared
volume cache keeps reruns cheap. It can take a few minutes; the command prints
progress.

### Step 11 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network:

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up gmail + linkedin + messages).

### Step 12 — Index the merged network

Index the merged people.csv on Modal (generic indexer, no import stage) and
download the duckdb:

```bash
cd "$REPO" && uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Run it in the background and keep Step 12 `in_progress` until the command
**exits 0**. This stage is long and mostly quiet — set expectations and don't
panic:

- **Expect 5–30+ minutes.** Most of the work (embeddings, role/company
  classification, duckdb build) runs **server-side on Modal**, so the local
  process can print little or nothing for **many minutes at a stretch**. A long
  silence with the process still alive is **normal and expected — not a hang**.
  Do not interrupt it, do not retry, do not declare failure on quiet.
- **The authoritative signal is the process itself**, not a status file: it
  stays running until done and prints a final `{"status": "completed", ...}` on
  success. `index-people` writes its progress to
  `.powerpacks/runs/setup-gmail-modal/status.json` (stages `enriching` →
  `importing` → `indexing` → `completed`) — poll that, **not**
  `setup-linkedin-modal/status.json`, which belongs to a different (prod
  `pipeline`) run and will look stale/"importing" forever here. If the gmail
  status file lags the live stdout, **trust the running process and its stdout.**
- **Do not treat pre-existing files in `.powerpacks/search-index/` as this run's
  output.** They may be left over from a prior run. The index is done only when
  the command exits 0 and has freshly downloaded `local-search.duckdb` +
  `manifest.json`. Confirm with Step 13, not by eyeballing the directory.

While it runs, reassure the user in plain language every poll, e.g. "Still
indexing on Modal (~N min in) — this stage is quiet by design; the job is alive
and I'm waiting for it to finish." Then proceed to Step 13 once it exits 0.

### Step 13 — Validate the search index

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

JSON with `status` (`ok`/`fail`/`missing`), per-table row counts,
`total_people`, `summary`. Pass only on `status: ok` (exit 0); on `fail`/
`missing` (exit 1) report the `errors`. Echo the `summary`.

---

## Done

Report a terse summary: logged in as <email>, keys pulled, N Gmail accounts
synced, LinkedIn imported, merged network of M people, index validated. Remind
the user that rerunning `$setup` reruns the whole checklist (Gmail re-syncs
incrementally; the msgvault db is preserved).
