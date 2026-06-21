---
name: setup
description: Deterministic LinkedIn-only Powerpacks setup. Use for $setup. Steps through update, Powerset login, runtime-key pull, LinkedIn Connections.csv import (on Modal), source merge, Modal index, and search-index validation. Always reruns the full checklist; overwrites in place. For Gmail use $import-gmail; for iMessage/WhatsApp use $import-messages.
---

<!--
Created: 2026-06-17
Changelog:
- 2026-06-17: Rewrote setup as a deterministic, rerunnable checklist.
- 2026-06-17: Promoted to a unified LinkedIn+Gmail+Messages multi-source flow.
- 2026-06-20: Split back to LinkedIn-only. Gmail moved to $import-gmail and
  iMessage/WhatsApp to $import-messages (each does its source + fan-in merge +
  Modal index). $setup now: update -> login -> keys -> import LinkedIn ->
  fan-in -> index -> validate. Uses the modern import -> fan-in -> index path so
  the $import-* skills can merge their sources on top of LinkedIn.
-->

# setup

`$setup` connects **LinkedIn** and builds one local search index: update, Powerset
login, runtime keys, then import your LinkedIn `Connections.csv` (enriched on
Modal), merge, index, and validate.

It runs a **fixed checklist and always reruns it end to end**. Reruns are
idempotent against fixed paths; rely on the primitives to overwrite.

**Other sources are their own skills** (each adds its source on top of whatever
is already imported, then re-merges + re-indexes):
- **Gmail** → `$import-gmail`
- **iMessage / WhatsApp** → `$import-messages`

## How to run this skill

**FIRST, before running anything: create a literal, visible checklist with all
eight steps below and step through it, marking each item complete as you go.**
Mandatory. Use your harness's plan/todo/task tool:

- **Claude Code:** `TaskCreate` one task per step (0–7), then `TaskUpdate` each
  to `in_progress` then `completed`.
- **Codex:** `update_plan` with the eight steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

Seed the checklist with these exact item titles:

```
0. Update Powerpacks (bin/update-codex)
1. Check Powerset login + credentials
2. Log in to Powerset (only if not logged in)
3. Pull runtime keys (Modal + OpenAI)
4. Import LinkedIn Connections.csv
5. Merge all sources
6. Index the merged network
7. Validate the search index
```

Step 2 is conditional (skip as a no-op if Step 1 says already logged in) — keep
it in the checklist and mark it complete.

Then:

1. **Work the checklist in order 0 → 7.** Exactly one item `in_progress` at a
   time; mark it `completed` before the next. No batching, reordering, skipping.
   One-line result per step.
2. **Run from the canonical repo root.** Resolve once and `cd` there (see *Repo
   root*). `.powerpacks/...` paths are relative to that root.
3. **Deterministic & in-place.** Overwrite the fixed derived paths
   (`.powerpacks/network-import/...`, `.powerpacks/search-index/...`); rely on
   the primitives to overwrite. Don't pre-delete with `rm` and don't invent
   timestamped/alternate folders.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** This skill is self-contained and
  authoritative. Do not read agent memory, prior state, other docs, or primitive
  source to re-derive paths. Build the checklist and execute it directly.
- **Do not edit code.** Only if you hit an actual blocking bug, and say so.
  Otherwise only invoke the primitives below.
- **Do not write scripts to do the work.** Reuse the exact primitive commands.
  Plain shell for `cp`/`test`/`wc`/`cat` is fine.
- **Consent gate (pause for the user):** Powerset browser login (Step 2). The
  LinkedIn import runs enrichment on Modal (no local key, no extra spend prompt).
  Everything else runs without asking.

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

### Step 4 — Import LinkedIn Connections.csv

Ask the user for their `Connections.csv` path. Place it at the canonical input
(overwrite), then enrich it **on Modal** — the same shared enrichment + cache
prod uses — which writes the enriched people.csv to the path the merge reads:

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

### Step 5 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network
(LinkedIn here; also Gmail/Messages if you've run those skills):

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up every imported source).

### Step 6 — Index the merged network

Index the merged people.csv on Modal (generic indexer, no import stage) and
download the duckdb:

```bash
cd "$REPO" && uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Run it in the background and keep Step 6 `in_progress` until the command
**exits 0**. This stage is long and mostly quiet — set expectations and don't
panic:

- **Expect 5–30+ minutes.** Most of the work (embeddings, role/company
  classification, duckdb build) runs **server-side on Modal**, so the local
  process can print little or nothing for **many minutes at a stretch**. A long
  silence with the process still alive is **normal and expected — not a hang**.
  Do not interrupt it, do not retry, do not declare failure on quiet.
- **The authoritative signal is the process itself**, not a status file: it stays
  running until done and prints a final `{"status": "completed", ...}` on
  success. `index-people` writes progress to
  `.powerpacks/runs/setup-gmail-modal/status.json` (stages `enriching` →
  `importing` → `indexing` → `completed`) — poll that, but if it lags the live
  stdout, **trust the running process and its stdout.**
- **Do not treat pre-existing files in `.powerpacks/search-index/` as this run's
  output.** They may be left over from a prior run. The index is done only when
  the command exits 0 and has freshly downloaded `local-search.duckdb` +
  `manifest.json`. Confirm with Step 7, not by eyeballing the directory.

While it runs, reassure the user every poll, e.g. "Still indexing on Modal
(~N min in) — quiet by design; the job is alive." Then proceed to Step 7 once it
exits 0.

### Step 7 — Validate the search index

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

JSON with `status` (`ok`/`fail`/`missing`), per-table row counts,
`total_people`, `summary`. Pass only on `status: ok` (exit 0); on `fail`/
`missing` (exit 1) report the `errors`. Echo the `summary`.

---

## Done

Report a terse summary: logged in as <email>, keys pulled, LinkedIn imported,
merged network of M people, index validated. Remind the user that rerunning
`$setup` reruns the whole checklist, and that **Gmail** (`$import-gmail`) and
**iMessage/WhatsApp** (`$import-messages`) are separate skills that add their
source on top and re-merge + re-index.
