---
name: setup
description: Deterministic one-command Powerpacks setup. Use for $setup. Steps through update, Powerset login, runtime-key pull, LinkedIn Connections.csv upload + process, and search-index validation. Always reruns the full checklist; overwrites in place.
---

<!--
Created: 2026-06-17
Changelog:
- 2026-06-17: Rewrote setup as a deterministic, rerunnable checklist that steps
  through update -> login -> pull keys -> upload Connections.csv -> process ->
  validate index, wired 1:1 to the primitives the local console buttons call.
  Replaces the prior phase-model/ledger/fan-out skill.
-->

# setup

`$setup` runs a fixed checklist and **always reruns it end to end**. Every call
repeats every step against the same fixed paths and lets the primitives
overwrite. There are no phases to skip, no resume logic, no "already done"
short-circuits. If the user runs `$setup` again, you run the whole checklist
again.

## How to run this skill

**FIRST, before running anything: create a literal, visible checklist with all
nine steps below and step through it, marking each item complete as you go.**
This is mandatory, not optional. Use your harness's plan/todo/task tool:

- **Claude Code:** `TaskCreate` one task per step (0–8), then `TaskUpdate` each
  to `in_progress` when you start it and `completed` when it finishes.
- **Codex:** call `update_plan` with the nine steps, and update the plan to mark
  each step `in_progress` then `completed` as you go.
- **Any other harness:** use its equivalent todo/plan mechanism so the user can
  see the checklist and watch each item get checked off.

Seed the checklist with these exact item titles:

```
0. Update Powerpacks (bin/update-codex)
1. Check Powerset login + credentials
2. Log in to Powerset (only if not logged in)
3. Pull runtime keys (Modal + OpenAI)
4. Ask for the LinkedIn Connections.csv
5. Import the CSV
6. Report processing estimate
7. Process contacts (enrich -> index -> download)
8. Validate the search index downloaded
```

Then:

1. **Work the checklist in order 0 → 8.** Exactly one item `in_progress` at a
   time; mark it `completed` before starting the next. Do not batch, reorder, or
   skip. Report a one-line result per step.
2. **Run from the canonical repo root.** Resolve it once and `cd` there for
   every command (see *Repo root* below). All `.powerpacks/...` paths in this
   doc are relative to that root — e.g. if Powerpacks is installed at
   `~/powerpacks`, the LinkedIn input lives at
   `~/powerpacks/.powerpacks/network-import/discover/linkedin/Connections.csv`.
3. **Deterministic & in-place.** Rely on the primitives to overwrite the fixed
   paths — `cp` replaces the canonical CSV, and the Modal pipeline replaces
   `local-search.duckdb`/`manifest.json` on download (it renames the prior copy
   to `.bkup`, so the canonical file is always fresh). Don't pre-delete with
   `rm`, and don't invent timestamped files, run ids, or alternate folders.
   Write only to the fixed paths named here.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** This skill is self-contained and
  authoritative. Do not read agent memory, prior setup state/ledgers, other
  docs, or the primitive source to re-derive paths or "expected behavior," and
  do not narrate a "quick context pass." The commands and fixed paths here are
  the source of truth — build the checklist and execute it directly.
- **Do not edit code.** Only edit a source file if you find an actual bug that
  blocks a step, and say so explicitly. Otherwise, only invoke the existing
  primitives below.
- **Do not write scripts to do the work.** No glue scripts, no new Python. Reuse
  the exact primitive commands listed. Plain shell for `cp`/`rm`/`test`/`wc` is
  fine.
- **Ask only for consent-bearing steps.** Powerset browser login is the only
  step that needs a human; pause there. Everything else runs without asking
  (key pull, upload, link, process, validation).

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

If the only checkout is under `.codex`, stop and ask the user to install/copy
Powerpacks to `~/powerpacks` (or set `POWERPACKS_REPO_ROOT`).

---

## The checklist

### Step 0 — Update Powerpacks

```bash
cd "$REPO" && bin/update-codex
```

Refreshes the checkout and reinstalls skills. Proceed when it exits 0.

### Step 1 — Check Powerset login + credentials

```bash
test -f "$HOME/.powerpacks/credentials.json" && echo "credentials.json: present" || echo "credentials.json: MISSING"
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py whoami
```

- `~/.powerpacks/credentials.json` is the Auth0 token store (home dir, **not**
  under the repo).
- `whoami` exits 0 and prints credential metadata when logged in. If the file is
  missing or `whoami` fails/exits non-zero, treat the user as **not logged in**
  → go to Step 2. Otherwise mark Step 2 done (no-op) and go to Step 3.

### Step 2 — Log in (only if Step 1 said not logged in)

This is the one consent step. It opens the system browser for the Auth0 flow.

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py login
```

(This is exactly what the console's login button runs via
`/local-api/powerset/login`.) If the browser cannot open from the shell, the
command prints a URL — give it to the user. Re-run `auth.py whoami` to confirm
success before continuing.

### Step 3 — Pull provisioned runtime keys (Modal + OpenAI)

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull --env-file .env
```

Fetches `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, and `OPENAI_API_KEY` from the
Powerset API and upserts them into `<repo>/.env`. (Same as the console's
`/local-api/powerset/pull-keys` button.) Requires Step 1/2 login. Verify with:

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py check --env-file .env
```

### Step 4 — Ask for the LinkedIn Connections.csv

Ask the user for the path to their LinkedIn `Connections.csv` export. Wait for a
real path; do not guess. Confirm the file exists before Step 5.

### Step 5 — Import the CSV

Place the CSV at the single canonical input path the console's **"Re-upload
Connections.csv"** flow ultimately points the pipeline at, overwriting any prior
copy, then link it.

```bash
cd "$REPO"
DEST=".powerpacks/network-import/discover/linkedin/Connections.csv"
mkdir -p "$(dirname "$DEST")"
cp -f "<user-csv-path>" "$DEST"     # overwrite canonical input in place
uv run --project . python packs/ingestion/primitives/setup_linkedin_csv/setup_linkedin_csv.py link --csv "$DEST"
```

`setup_linkedin_csv.py link` writes `csv_path` + `linked=true` into
`.powerpacks/ingestion/accounts.json` (it does not process). The canonical path
`.powerpacks/network-import/discover/linkedin/Connections.csv` is the same stable
input the console uses (`onboarding.ts` `stableConnectionsCsv`).

### Step 6 — Tell the user the processing estimate, then auto-continue

Count the connections and print a concrete minute estimate, then move straight
to Step 7 without waiting for confirmation. Always give an actual number — never
say "a few minutes" without one.

```bash
cd "$REPO"
rows=$(( $(wc -l < .powerpacks/network-import/discover/linkedin/Connections.csv) - 1 ))
mins=$(( (100 + rows * 45 / 100) / 60 + 1 ))
echo "connections: $rows  estimate: ~${mins} min"
```

Tell the user the number, e.g. "Processing your N connections — estimated
~M minutes (may finish sooner if cached). I'll keep going." Then proceed.

### Step 7 — Process contacts (enrich → index → download)

Announce this step in one terse line — e.g. "Starting Step 7 — enriching
contacts and building the search index." Do not explain `--force`,
determinism, or hashes to the user.

Run the **"Process contacts"** pipeline with `--force` so it reprocesses
deterministically instead of no-opping on an unchanged-CSV hash. The pipeline
overwrites the local index itself (renames the prior copy to `.bkup`), so there
is nothing to delete first.

```bash
cd "$REPO"
uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py pipeline \
  --csv .powerpacks/network-import/discover/linkedin/Connections.csv \
  --source-user linkedin \
  --force
```

**This step can take 5–30 minutes depending on network size — do not assume it
hung.** Run the pipeline in the background and poll its progress about **every
60s**, reporting the current phase to the user, until it exits. The pipeline
writes live status to `.powerpacks/runs/setup-linkedin-modal/status.json` (phase
`importing` → `indexing` → `completed`):

```bash
cd "$REPO" && cat .powerpacks/runs/setup-linkedin-modal/status.json 2>/dev/null
```

Keep Step 7 `in_progress` while polling; only mark it complete when the pipeline
exits 0. Do not poll faster than 60s.

This is exactly the console's `/local-api/onboarding/linkedin/run` command
(`onboardingV3PipelineCommand`) plus `--force`. It runs the two Modal stages —
**Importing contacts** (RapidAPI enrich) then **Building search index** — and
downloads `local-search.duckdb` + `manifest.json` into
`.powerpacks/search-index/`. It can take a while (job timeout is generous);
let it finish.

### Step 8 — Validate the search index downloaded

```bash
cd "$REPO"
ls -lh .powerpacks/search-index/local-search.duckdb .powerpacks/search-index/manifest.json
```

The step passes when both files exist and `local-search.duckdb` is a non-trivial
size. If `local-search.duckdb` is missing after Step 7 succeeded, surface the
pipeline output — do not silently retry. Report local search as ready.

---

## Done

Report a terse summary: logged in as <email>, keys pulled, N connections
processed, index at `.powerpacks/search-index/local-search.duckdb` (size).
Remind the user that running `$setup` again reruns the entire checklist and
overwrites in place.
